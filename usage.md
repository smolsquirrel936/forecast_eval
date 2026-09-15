# usage.md

Practical guide for running `forecast_eval`. For the architecture and design rationale see [SPEC.md](SPEC.md); for environment / interpreter notes see [CLAUDE.md](CLAUDE.md).

> **The harness** — used throughout this doc — refers to the backtest engine in [run.py](run.py) (`run_backtest()` and its per-tick event loop wiring `Environment → Execution → Trader → Forecaster → SignalEmitter → Portfolio`). Calling `Toto2Forecaster.forecast()` directly bypasses the harness — no orders, no fills, no PnL accounting; see §8 for the direct-drive pattern.

All commands below assume your shell is in **`night/`** (the parent of this folder). This repo uses a local `uv` venv (`.venv/`, Python 3.12) — prefix every command with `uv run --no-sync`, which uses it without touching it. **Never plain `uv run`, `uv sync`, `uv add` or `uv remove`:** `pyproject.toml` declares only `shioaji`, so plain `uv run` rebuilds an incompatible `.venv/` with shioaji alone, and `uv sync` removes every hand-installed package (torch, toto-2, pandas, pytest). See [AGENTS.md](../AGENTS.md) and [UV_SETUP.md](../UV_SETUP.md) for setup; `torch` must come from the cu130 index (RTX 5090 / Blackwell, sm_120).

---

## 1. Quick orientation

There are **three entry points** plus a test suite:

| Entry point | What it does | Needs Toto2? |
|---|---|---|
| [run.py](run.py) — `uv run --no-sync python -m forecast_eval.run` | Runs Phases 1–4 demos back-to-back on synthetic ticks | No |
| [test_toto2.py](test_toto2.py) — `uv run --no-sync python -m forecast_eval.test_toto2` | One-shot smoke test: load checkpoint, run one forecast | Yes |
| [real_data.py](real_data.py) — `uv run --no-sync python -m forecast_eval.real_data` | Full backtest of Toto2 on real TXF 1-min bars (whole dataset, warmup split) | Yes |
| `pytest forecast_eval/tests/` | 98 unit + integration tests (~7 s) | No |

Reports / charts are generated **automatically** at the end of every backtest by [reports.py](reports.py) (via the `auto_report=True` hook in `write_all`). You can also re-run [reports.py](reports.py) on any existing run directory to regenerate them — see §7.

---

## 2. Running tests

```bash
uv run --no-sync python -m pytest forecast_eval/tests/ -v
```

Run a single file or test:

```bash
uv run --no-sync python -m pytest forecast_eval/tests/test_execution.py -v
uv run --no-sync python -m pytest forecast_eval/tests/test_execution.py::test_marketable_buy_fills_at_next_print_price -v
```

The suite covers the no-flip rule, marketable / passive fill rules, look-ahead defense, round-trip pairing, metric math, and end-to-end PnL on known data. See [tests/](tests/) for what's covered.

---

## 3. Synthetic backtest demos ([run.py](run.py))

Synthetic random-walk ticks; no model, no real data, runs in seconds. Useful for sanity-checking changes to the trader / execution / metrics code.

```bash
uv run --no-sync python -m forecast_eval.run
```

Prints summaries for four demos:

1. **Phase 1** — dummy alternating signal (no forecaster)
2. **Phase 2** — `NaiveLastPrice` forecaster + `ThresholdEmitter`
3. **Phase 3** — alternating + 5-tick fixed stop-loss
4. **Phase 3** — alternating + DAY↔NIGHT forced session close

The script also calls `demo_with_logs_and_metrics()` which writes a full artifact bundle to `outputs/phase4_demo_<ts>/` (see §6).

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
uv run --no-sync python -m forecast_eval.test_toto2
```

Expected: a printed `=== Forecast ===` block with `last_price`, `predicted_price`, `predicted_return`, and the first / last 5 values of the `median_path`. If you see `[IMPORT ERROR]`, install `torch` and the local `toto2` package (see [CLAUDE.md](CLAUDE.md)).

Use this whenever you change [forecaster/toto2.py](forecaster/toto2.py) before running the slow real-data demo.

---

## 5. Real-data backtest ([real_data.py](real_data.py))

End-to-end backtest of Toto2 on real TXF 1-min OHLC data. Uses the whole deduped
file and splits it: the first `--lookback-frac` is silent warmup, the rest is the
backtest window. Defaults: context_length=3008, horizon=30, stride=30, matching
the toto2 notebook configuration.

### `--strategy day|night|fullday` — run a whole pinned deployment

Since the three-way split (STRATEGY_SPLIT_PLAN), one flag selects a session's
entire strategy set — its Forecaster, SignalEmitter and Trader, from
`strategy/<session>/` — and supplies the twelve knobs that deployment was tuned
with.

```bash
# NIGHT's pinned configuration: Calmar 3.6006, 235 trades
uv run --no-sync python -m forecast_eval.real_data --strategy night --precompute \
    --data "\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_night.csv" \
    --warmup-bars 538790

# DAY's: Calmar 1.6937, 67 trades
uv run --no-sync python -m forecast_eval.real_data --strategy day --precompute \
    --data "\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv" \
    --warmup-bars 676123
```

| session | tape | baseline |
|---|---|---|
| `night` | `TXF_RPT_minute_night.csv` | Calmar 3.6006, 235 trades |
| `day` | `TXF_RPT_minute_day.csv` | Calmar 1.6937, 67 trades |
| `fullday` | `TXF_RPT_minute_fullday.csv` — **not** the combined tape | **untuned**, no baseline |

- **An explicit flag still beats `--strategy`.** The banner prints which knob came
  from where, so `--strategy night --buy-threshold 0.0005` runs 0.0005 and says so.
- **Omitting `--strategy` changes nothing.** The CLI's own historical defaults
  apply, so every command string recorded in `results.tsv` reproduces exactly.
- The run records `strategy` and `forecaster_sha` into `params.json`. The sha is
  necessary, but not sufficient, for two `results.tsv` rows to share a forecast stream.
  It hashes `forecaster.py` only, so `checkpoint`, `context_length`, `horizon_bars` and
  `stride_bars` must match too; changing any of them moves the stream without moving the
  sha (see [docs/strategy/wiring.md](docs/strategy/wiring.md)).

```bash
# defaults (313m checkpoint, full dataset, 30% warmup)
uv run --no-sync python -m forecast_eval.real_data

# batch forecasts on the GPU up front, then replay execution
uv run --no-sync python -m forecast_eval.real_data --precompute \
    --data "\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv" --lookback-frac 0.5

# tweak thresholds
uv run --no-sync python -m forecast_eval.real_data \
    --buy-threshold 0.0002 --sell-threshold -0.0002

# different checkpoint + probability gating
uv run --no-sync python -m forecast_eval.real_data \
    --checkpoint Datadog/Toto-2.0-1B --min-prob 0.61

# enable session-boundary forced close
uv run --no-sync python -m forecast_eval.real_data --forced-close
```

Key CLI flags (see `--help` for the full list):

| Flag | Default | Meaning |
|---|---|---|
| `--data` | `\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv` | Source 1-min CSV (night session: `TXF_RPT_minute_night.csv`) |
| `--n-bars` | None (full) | Trailing rows to use |
| `--lookback-frac` | 0.30 | Silent-warmup fraction |
| `--checkpoint` | `Datadog/Toto-2.0-313m` | HuggingFace checkpoint id |
| `--context-length` | 3008 | Bars fed to the model per inference |
| `--horizon-bars` | 30 | Forecast horizon |
| `--stride-bars` | 30 | Re-forecast every N bars |
| `--buy-threshold` | 0.0005 | Predicted return above this → BUY |
| `--sell-threshold` | -0.0005 | Predicted return below this → SELL |
| `--min-prob` | None | Optional directional-probability gate |
| `--full-confidence-prob` | 0.9 | Where conviction `strength` saturates |
| `--max-aggression-ticks` | None | Aggression ceiling for confidence-scaled offset |
| `--max-position` | 1 | Max contracts for confidence-scaled sizing |
| `--fee-rate` | 0.0002 | Per-side rate |
| `--forced-close` | False | Flatten at DAY↔NIGHT boundaries |
| `--precompute` | False | Batch all forecasts on the GPU, then replay |
| `--output-dir` | (auto) | Defaults to `outputs/real_data_<ts>/` |

Output: parquet artifacts + `params.json` under `outputs/real_data_<ts>/` (see §6) and three blocks of metrics printed to stdout (trading, forecast quality, attribution, plus a buy-and-hold floor).

---

## 6. Output layout

Every run that calls [logging_io.write_all](logging_io.py) writes the same five (or six) files:

```
outputs/<run_name>_<YYYYMMDD_HHMMSS>/
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
    trades = pd.read_parquet("outputs/real_data_20260525_003535/trades.parquet")
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
uv run --no-sync python -m forecast_eval.reports outputs/real_data_20260525_003535

# overlay the real price series on the price-fills chart
uv run --no-sync python -m forecast_eval.reports outputs/real_data_20260525_003535 \
    --data "\\DESKTOP-SQUIRRE\dataset\tick\TXF_OHLC_1min.csv"
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

## 8. Programmatic use — Toto2 without the harness

If you want to drive Toto2 directly (bypassing the harness — no orders / fills / PnL):

```python
import pandas as pd
from forecast_eval.forecaster.toto2 import Toto2Forecaster

bars = pd.read_csv(r"\\DESKTOP-SQUIRRE\dataset\tick\TXF_OHLC_1min.csv").dropna(subset=["close"])
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

## 9. Plugging in your own forecaster or emitter

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
    tick_size=1.0, aggression_ticks=3, fee_rate=0.0002,
    forced_close_on_session_end=True,
)
```

For sweeps where the emitter or forecaster carries per-run state (iterator cycles, model caches), use [run.py:fee_sensitivity_sweep](run.py)'s `make_emitter=` / `make_forecaster=` factory pattern — passing instances directly will silently reuse state across iterations.

---

## 10. Computing metrics standalone

Given a `BacktestResult` from `run_backtest()`:

```python
from forecast_eval.metrics import compute_metrics, buy_and_hold_pnl

metrics = compute_metrics(res, ticks, forced_close=False)
print(metrics["trading"])      # round-trip Sharpe/Sortino, fee drag, drawdown
print(metrics["forecast"])     # hit rate, Spearman IC
print(metrics["attribution"])  # signal PnL - realized PnL = execution drag

print(buy_and_hold_pnl(ticks, fee_rate=0.0002))
```

When `forced_close=True`, `metrics["trading"]` is replaced by `metrics["trading_day"]` + `metrics["trading_night"]`. See [metrics.py](metrics.py) for what each field means.

---

## 11. Common gotchas

* **Run from `night/`, not from inside `forecast_eval/`.** The `uv run --no-sync python -m forecast_eval.xxx` form requires the parent. Direct execution (`python real_data.py`) only works for files with the `sys.path.insert(parent.parent)` trampoline at the top.
* **Use `uv run --no-sync` (the local `.venv`, Python 3.12) for anything touching `toto2`, `lightning`, or `pytest`.** Plain `uv run` can rebuild `.venv/` and drop those packages. See [AGENTS.md](../AGENTS.md) / [UV_SETUP.md](../UV_SETUP.md).
* **NaN rows depend entirely on which file you load** (measured 2026-09-01; an earlier "~17 %" here was right about neither). The legacy `tick/TXF_OHLC_1min.csv` is **47.9 % NaN** — it carries a row per calendar minute, most of which the market was closed for. The four `RPT/` minute tapes carry **0 NaN closes**: they hold only minutes that actually printed. [real_data.py:load_minute_bars](real_data.py) drops NaN closes either way, which matters because Toto2 propagates them.
* **`context_length` must divide the model's `patch_size`** ([forecaster/toto2.py](forecaster/toto2.py) truncates automatically, but if you see a shape mismatch this is why).
* **No-flip rule** ([SPEC.md §4.2](SPEC.md)): a SELL while long closes only — it does not flip to short. Same for BUY while short. Pyramiding is also disabled.
* **Reusing stateful emitters / forecasters across sweep iterations is silently wrong.** Use factories (`make_emitter=lambda: ...`) for any object with per-run state.
