"""Compare Toto2 checkpoint sizes on real TXF 1-min data — with backtesting.

Per checkpoint, this script runs **two** passes on the same data:

1. **Forecast-quality eval** — N evenly-spaced windows, recording predicted
   return / realized return / inference latency. Cheap, one row per window.

2. **Full backtest** — runs the per-tick event loop in [run.py](run.py) with
   ``Toto2Forecaster + ThresholdEmitter``, writes the standard parquet
   artifact bundle (``fills``, ``orders``, ``forecasts``, ``signals``,
   ``trades``) to a per-checkpoint subdirectory, and reports trading
   metrics (net PnL, Sharpe, drawdown, n_trades).

For each checkpoint in 4m / 22m / 313m / 1B / 2.5B:
  * load the model (timed; happens on the first forecast call)
  * run N evaluation windows
  * run a full backtest and persist fills / trades / signals / orders / forecasts
  * tear down to free VRAM before the next checkpoint

Outputs (under ``outputs/compare_models_<ts>/``):
  * ``per_window.csv``      — one row per (checkpoint × window)
  * ``summary.csv``         — one row per checkpoint with forecast quality
                              AND backtest trading metrics
  * ``params.json``         — invocation hyperparameters + any failures
  * ``<ckpt>/...parquet``   — per-checkpoint full backtest artifacts
                              (skipped when ``--skip-backtest`` is passed)

Run from myPaper/:
    python -m forecast_eval.compare_models
    python -m forecast_eval.compare_models --n-windows 30 --n-bars 8000
    python -m forecast_eval.compare_models --checkpoints Datadog/Toto-2.0-4m Datadog/Toto-2.0-313m
    python -m forecast_eval.compare_models --skip-backtest   # forecast-only mode
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Allow direct execution from inside the folder.
# Tech: put the repo root on sys.path before the package imports.
# Why:  the trampoline that lets the file run both via `python -m` and directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from forecast_eval.forecaster.toto2 import Toto2Forecaster
from forecast_eval.logging_io import DEFAULT_OUTPUT_ROOT, write_all, write_params
from forecast_eval.metrics import compute_metrics
from forecast_eval.real_data_demo import DEFAULT_DATA, load_minute_bars
from forecast_eval.run import run_backtest
from forecast_eval.strategy.threshold import ThresholdEmitter


# Tech: the five Datadog Toto-2.0 checkpoints from smallest to largest.
# Why:  the whole point of the sweep is to see whether bigger models convert IC
#       into realized PnL; ordering small→large means quick checkpoints run first
#       and a crash on the 2.5B doesn't cost the cheaper results.
DEFAULT_CHECKPOINTS = [
    "Datadog/Toto-2.0-4m",
    "Datadog/Toto-2.0-22m",
    "Datadog/Toto-2.0-313m",
    "Datadog/Toto-2.0-1B",
    "Datadog/Toto-2.0-2.5B",
]


def _sign(x: float) -> int:
    # Tech: return -1/0/+1 for the sign of x.
    # Why:  direction hit rate compares the *sign* of predicted vs. realized return;
    #       a tiny helper keeps that comparison readable in summarize().
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def _free_vram() -> None:
    """Best-effort CUDA cache release between checkpoints."""
    # Tech: if torch+CUDA are present, empty the allocator cache; swallow anything.
    # Why:  successive large checkpoints would otherwise accumulate VRAM and OOM the
    #       GPU; this is best-effort (CPU-only or no-torch runs simply no-op), so any
    #       error here must not abort the sweep.
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _safe_ckpt_name(checkpoint: str) -> str:
    """Turn ``Datadog/Toto-2.0-313m`` into ``Datadog_Toto-2.0-313m`` for paths."""
    # Tech: replace path separators with underscores.
    # Why:  a HuggingFace id contains '/', which would otherwise create unwanted
    #       nested directories; flattening it makes one clean per-checkpoint subdir.
    return checkpoint.replace("/", "_").replace("\\", "_")


def _per_window_eval(
    fc: Toto2Forecaster,
    bars: pd.DataFrame,
    *,
    checkpoint: str,
    windows: list[int],
    horizon: int,
) -> list[dict]:
    """Phase A: evaluate predicted vs. realized return at each window index."""
    rows = []
    for i, t_idx in enumerate(windows):
        # Tech: forecast from history up to and including t_idx, timing the call.
        # Why:  slicing :t_idx+1 enforces look-ahead safety (the model sees nothing
        #       after t); timing each call gives the per-window inference latency the
        #       summary reports, isolated from the one-time model load.
        history = bars.iloc[: t_idx + 1]
        t0 = time.time()
        f = fc.forecast(history)
        dt = time.time() - t0

        last_price = float(f.payload["last_price"])
        predicted_return = float(f.payload["predicted_return"])
        predicted_price = float(f.payload["predicted_price"])

        # Tech: read the realized price `horizon` bars ahead and compute its return;
        #       NaN when the horizon runs past the end of the data.
        # Why:  the realized move is what we score the prediction against; guarding
        #       the tail avoids an index error on the last few windows and marks them
        #       unscored rather than fabricating a number.
        target_idx = t_idx + horizon
        if target_idx < len(bars):
            realized_price = float(bars["price"].iloc[target_idx])
            realized_return = (realized_price - last_price) / last_price
        else:
            realized_price = float("nan")
            realized_return = float("nan")

        # Tech: record one row capturing prediction, realization, and latency, and
        #       print a compact progress line.
        # Why:  per_window.csv is the raw material summarize() aggregates; the printed
        #       line gives live feedback during a long multi-checkpoint sweep.
        rows.append({
            "checkpoint": checkpoint,
            "window_idx": i,
            "t_idx": t_idx,
            "timestamp": history["timestamp"].iloc[-1],
            "last_price": last_price,
            "predicted_price": predicted_price,
            "predicted_return": predicted_return,
            "realized_price": realized_price,
            "realized_return": realized_return,
            "latency_s": dt,
        })
        print(
            f"  window {i + 1:>2}/{len(windows)}: "
            f"pred={predicted_return:+.5f}  real={realized_return:+.5f}  "
            f"({dt * 1000:.0f} ms)"
        )
    return rows


def _run_backtest_for_checkpoint(
    fc: Toto2Forecaster,
    bars: pd.DataFrame,
    *,
    checkpoint: str,
    args: argparse.Namespace,
    output_root: Path,
) -> dict:
    """Phase B: full backtest using fc + ThresholdEmitter; writes parquet bundle."""
    # Tech: build the emitter from the CLI thresholds and pick the per-checkpoint
    #       output subdir.
    # Why:  reusing the *same* fc instance that ran Phase A avoids a second model
    #       load; the flattened checkpoint name keeps each model's artifacts isolated.
    em = ThresholdEmitter(
        buy_threshold=args.buy_threshold,
        sell_threshold=args.sell_threshold,
    )
    ckpt_dir = output_root / _safe_ckpt_name(checkpoint)

    # Tech: run the full harness backtest over all bars, timing it, and print counts.
    # Why:  this is the realized-PnL pass (Phase A only measured forecast quality);
    #       same knobs as real_data_demo so the two entry points stay comparable.
    t0 = time.time()
    res = run_backtest(
        bars,
        forecaster=fc,
        emitter=em,
        tick_size=1.0,
        aggression_ticks=args.aggression_ticks,
        fee_rate=args.fee_rate,
        contract_multiplier=args.contract_multiplier,
        forced_close_on_session_end=args.forced_close,
    )
    elapsed = time.time() - t0
    print(
        f"  backtest: {elapsed:.1f}s  "
        f"({res.n_forecasts} forecasts, {res.n_orders} orders, {len(res.fills)} fills)"
    )

    # Tech: compute the metric pack before writing, so the auto-generated report can
    #       open with the Result-summary tables; the same dict feeds the bt_* flatten.
    # Why:  write_all triggers generate_report, which now renders these metrics — they
    #       must exist beforehand. Computing once here avoids any recompute.
    metrics = compute_metrics(res, bars, forced_close=args.forced_close)

    # Tech: write the standard artifact bundle for this checkpoint (+ auto charts).
    # Why:  every checkpoint gets the same parquet bundle so results are diffable;
    #       passing the data path lets the per-checkpoint price chart show the real series.
    paths = write_all(
        res,
        ckpt_dir,
        params={**vars(args), "checkpoint": checkpoint},
        report_data_path=args.data,
        report_ticks=bars,
        metrics=metrics,
    )
    for name, p in paths.items():
        print(f"    {name:10s} -> {p}")

    # Tech: flatten the trading block(s) into bt_* fields — a single 'trading' block
    #       normally, or DAY+NIGHT combined under forced close.
    # Why:  summary.csv is one row per checkpoint, so the (possibly two) session
    #       blocks must collapse to scalar columns; net PnL sums and drawdown takes the
    #       worse of the two, while Sharpe is left NaN because it isn't additively
    #       combinable across sessions.
    # When forced_close=True there are trading_day / trading_night blocks; otherwise
    # a single 'trading' block. Aggregate into one flat dict for the summary row.
    trading_blocks = {k: v for k, v in metrics.items() if k.startswith("trading")}
    if "trading" in trading_blocks:
        tb = trading_blocks["trading"]
        flat = {
            "bt_net_pnl_points":    tb.get("net_pnl_points"),
            "bt_n_trades":          tb.get("n_trades"),
            "bt_sharpe_per_trade":  tb.get("sharpe_per_trade"),
            "bt_max_drawdown_pts":  tb.get("max_drawdown_points"),
        }
    else:
        # forced_close → DAY + NIGHT; combine via simple sum / weighted avg.
        day = trading_blocks.get("trading_day", {})
        nig = trading_blocks.get("trading_night", {})
        n_day = day.get("n_trades", 0) or 0
        n_nig = nig.get("n_trades", 0) or 0
        n_total = n_day + n_nig
        flat = {
            "bt_net_pnl_points":   (day.get("net_pnl_points", 0.0) or 0.0)
                                 + (nig.get("net_pnl_points", 0.0) or 0.0),
            "bt_n_trades":         n_total,
            "bt_sharpe_per_trade": float("nan"),  # not meaningfully combinable
            "bt_max_drawdown_pts": max(
                day.get("max_drawdown_points", 0.0) or 0.0,
                nig.get("max_drawdown_points", 0.0) or 0.0,
            ),
        }
    # Tech: attach the portfolio-level net, the elapsed time, and the output dir.
    # Why:  the portfolio net is a cross-check against the trade-derived net; elapsed
    #       and dir round out the row so summary.csv is self-contained for each model.
    flat["bt_realized_net_portfolio"] = float(res.portfolio.net_pnl())
    flat["bt_elapsed_s"] = float(elapsed)
    flat["bt_output_dir"] = str(ckpt_dir)
    return flat


def evaluate_checkpoint(
    bars: pd.DataFrame,
    checkpoint: str,
    *,
    args: argparse.Namespace,
    windows: list[int],
    output_root: Path,
) -> tuple[list[dict], float, dict]:
    """Run forecast-quality eval and (optionally) a full backtest for one checkpoint.

    Returns ``(per_window_rows, load_seconds, backtest_summary)``. The
    backtest summary is empty when ``--skip-backtest`` is set.
    """
    # Tech: construct a fresh Toto2Forecaster for this checkpoint.
    # Why:  a new instance per checkpoint guarantees no previous model stays pinned in
    #       VRAM and no cached weights leak across models; one instance then serves
    #       both Phase A and Phase B for this checkpoint (single load).
    print(f"\n[{checkpoint}]")
    fc = Toto2Forecaster(
        warmup_bars=args.context_length,
        forecast_stride_bars=args.stride_bars,
        forecast_horizon_bars=args.horizon_bars,
        context_length=args.context_length,
        checkpoint=checkpoint,
        device=args.device,
        bar_freq=None,
        signal_step="last",
    )

    # Warmup call — triggers checkpoint download (if not cached) + model load.
    # Separated so per-window latency below reflects pure inference.
    # Tech: do one throwaway forecast and time it as the "load" cost.
    # Why:  the first call pays the download + weight-load tax; isolating it here means
    #       the per-window latencies measured next are pure inference, not skewed by load.
    t_load = time.time()
    _ = fc.forecast(bars.iloc[: args.context_length])
    load_s = time.time() - t_load
    print(f"  load + first forecast : {load_s:.1f}s")

    # Tech: run the per-window forecast-quality eval (Phase A).
    # Why:  cheap pass that yields hit rate / IC / latency without any trading.
    rows = _per_window_eval(
        fc, bars,
        checkpoint=checkpoint,
        windows=windows,
        horizon=args.horizon_bars,
    )

    # Tech: optionally run the full backtest (Phase B) unless --skip-backtest.
    # Why:  the harness pass is the expensive part; skipping it gives a fast
    #       forecast-only mode for quick model-quality comparisons.
    backtest_summary: dict = {}
    if not args.skip_backtest:
        backtest_summary = _run_backtest_for_checkpoint(
            fc, bars,
            checkpoint=checkpoint,
            args=args,
            output_root=output_root,
        )

    # Tech: drop the forecaster and release VRAM before returning.
    # Why:  explicit teardown is what lets the sweep run all five checkpoints
    #       sequentially on one GPU without OOM (see usage.md §8).
    del fc
    _free_vram()
    return rows, load_s, backtest_summary


def summarize(
    df: pd.DataFrame,
    load_times: dict[str, float],
    backtest_summaries: dict[str, dict],
) -> pd.DataFrame:
    """One row per checkpoint: forecast quality + (optional) backtest metrics."""
    summaries = []
    for ckpt, g in df.groupby("checkpoint", sort=False):
        # Tech: keep only windows that have a realized return (drop the unscored tail).
        # Why:  windows whose horizon ran past the data end carry NaN realized return;
        #       including them would poison the mean/IC, so they're excluded from stats.
        valid = g.dropna(subset=["realized_return"])
        if valid.empty:
            continue
        pred = valid["predicted_return"].astype(float)
        real = valid["realized_return"].astype(float)
        # Tech: direction hit rate over windows where both returns are nonzero; the
        #       Spearman IC of predicted vs. realized when there are ≥2 points.
        # Why:  zero returns are non-directional bets and would distort the hit rate;
        #       IC needs at least two points to define a rank correlation.
        nonzero = (pred != 0) & (real != 0)
        if nonzero.any():
            hits = (pred[nonzero].map(_sign) == real[nonzero].map(_sign)).mean()
        else:
            hits = float("nan")
        ic = pred.corr(real, method="spearman") if len(pred) >= 2 else float("nan")
        # Tech: assemble the forecast-quality row, then merge in this checkpoint's
        #       backtest summary (empty under --skip-backtest).
        # Why:  putting forecast quality and realized trading metrics on one row is the
        #       whole purpose of the sweep — it's how IC-vs-PnL is read off directly.
        row = {
            "checkpoint": ckpt,
            "n_windows": int(len(valid)),
            "load_s": load_times.get(ckpt, float("nan")),
            "mean_latency_s": float(g["latency_s"].mean()),
            "direction_hit_rate": float(hits),
            "spearman_ic": float(ic),
            "mean_abs_return_err": float((pred - real).abs().mean()),
            "mean_pred_return": float(pred.mean()),
            "mean_real_return": float(real.mean()),
        }
        row.update(backtest_summaries.get(ckpt, {}))
        summaries.append(row)
    return pd.DataFrame(summaries)


def _pick_windows(n_bars: int, n_windows: int, context_length: int, horizon: int) -> list[int]:
    # Tech: choose evenly-spaced evaluation indices between the earliest forecastable
    #       bar (context_length) and the last one with a full horizon ahead.
    # Why:  windows must have both enough history (≥ context) and enough future (≥
    #       horizon) to be scorable; spacing them evenly samples the whole eval region
    #       rather than clustering, so quality isn't judged on one regime.
    start = context_length
    end = n_bars - horizon - 1
    if start >= end or n_windows <= 0:
        return []
    if n_windows == 1:
        return [end]
    step = max(1, (end - start) // (n_windows - 1))
    ws = list(range(start, end + 1, step))[:n_windows]
    # Make sure the last window is the tail (helps cover the most recent data).
    # Tech: force the final window to `end`.
    # Why:  integer stepping can stop short of the tail; pinning the last window to
    #       the most recent scorable bar keeps the newest data always represented.
    if ws[-1] != end:
        ws[-1] = end
    return ws


def main(argv=None) -> int:
    # Tech: define the CLI — data/window controls, model context/horizon/stride,
    #       device, checkpoint list, output dir, and the backtest knobs.
    # Why:  the backtest flags deliberately mirror real_data_demo so a sweep and a
    #       single run use the same parameter names; --skip-backtest gates the whole
    #       Phase B so the fast forecast-only mode is one flag away.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help="Path to TXF_OHLC_1min.csv")
    ap.add_argument("--n-bars", type=int, default=6000,
                    help="Trailing rows of the 1-min file to use")
    ap.add_argument("--n-windows", type=int, default=20,
                    help="Evaluation windows per checkpoint (evenly spaced)")
    ap.add_argument("--context-length", type=int, default=3008)
    ap.add_argument("--horizon-bars", type=int, default=30)
    ap.add_argument("--stride-bars", type=int, default=30,
                    help="Backtest re-forecast cadence in bars (default = horizon)")
    ap.add_argument("--device", default="auto",
                    help='"cuda", "cpu", or "auto"')
    ap.add_argument("--checkpoints", nargs="+", default=DEFAULT_CHECKPOINTS,
                    help="HuggingFace checkpoint ids to compare")
    ap.add_argument("--output-dir", type=Path, default=None,
                    help="Defaults to <myPaper>/outputs/compare_models_<timestamp>/")

    # Backtest knobs (mirror real_data_demo.py).
    ap.add_argument("--skip-backtest", action="store_true",
                    help="Forecast-quality eval only; skip the full backtest pass")
    ap.add_argument("--buy-threshold", type=float, default=0.0005,
                    help="Predicted return above this -> BUY")
    ap.add_argument("--sell-threshold", type=float, default=-0.0005,
                    help="Predicted return below this -> SELL")
    ap.add_argument("--aggression-ticks", type=int, default=0)
    ap.add_argument("--fee-rate", type=float, default=0.00015)
    ap.add_argument("--contract-multiplier", type=float, default=200.0)
    ap.add_argument("--forced-close", action="store_true",
                    help="Flatten at every DAY<->NIGHT session boundary")
    args = ap.parse_args(argv)

    # Tech: default the output dir to a timestamped sweep dir and create it now.
    # Why:  unlike single runs, the sweep writes top-level CSVs immediately, so the
    #       directory must exist up front (mkdir here, not deferred).
    if args.output_dir is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output_dir = DEFAULT_OUTPUT_ROOT / f"compare_models_{ts}"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Tech: fail fast if the data file is absent.
    # Why:  no point loading any model if there's nothing to evaluate on.
    if not args.data.exists():
        print(f"ERROR: data file not found at {args.data}", file=sys.stderr)
        return 2

    # Tech: load the shared bars and echo their shape/range.
    # Why:  every checkpoint evaluates on the *same* bars — loading once keeps the
    #       comparison apples-to-apples and avoids re-reading the file five times.
    print(f"loading: {args.data}")
    bars = load_minute_bars(args.data, args.n_bars)
    print(f"  rows  : {len(bars)}")
    print(f"  range : {bars['timestamp'].iloc[0]} -> {bars['timestamp'].iloc[-1]}")

    # Tech: compute the shared evaluation window indices; abort if none are valid.
    # Why:  all checkpoints must hit the identical windows for a fair comparison; an
    #       empty list means the data is too small for context+horizon, which is a
    #       hard stop with an explanatory message.
    windows = _pick_windows(
        n_bars=len(bars),
        n_windows=args.n_windows,
        context_length=args.context_length,
        horizon=args.horizon_bars,
    )
    if not windows:
        print(
            f"ERROR: not enough bars ({len(bars)}) for context_length="
            f"{args.context_length} + horizon={args.horizon_bars}",
            file=sys.stderr,
        )
        return 2
    print(f"evaluating {len(windows)} windows: t_idx {windows[0]} .. {windows[-1]}")
    print(f"checkpoints  : {args.checkpoints}")
    print(f"output_dir   : {args.output_dir}")
    print(f"backtest     : {'SKIPPED' if args.skip_backtest else 'ON'}"
          f"  (buy={args.buy_threshold}, sell={args.sell_threshold}, "
          f"fee={args.fee_rate}, forced_close={args.forced_close})")

    # Tech: iterate checkpoints, collecting per-window rows, load times, backtest
    #       summaries, and a failures map; a crash on one checkpoint is caught,
    #       logged, VRAM freed, and the sweep continues.
    # Why:  one bad/oversized checkpoint (e.g. 2.5B OOM) must not lose the results of
    #       the others; catching per-checkpoint and recording the reason keeps the
    #       sweep robust and the params.json honest about what failed.
    all_rows: list[dict] = []
    load_times: dict[str, float] = {}
    backtest_summaries: dict[str, dict] = {}
    failures: dict[str, str] = {}
    for ckpt in args.checkpoints:
        try:
            rows, load_s, bt_summary = evaluate_checkpoint(
                bars, ckpt,
                args=args,
                windows=windows,
                output_root=args.output_dir,
            )
            all_rows.extend(rows)
            load_times[ckpt] = load_s
            if bt_summary:
                backtest_summaries[ckpt] = bt_summary
        except Exception as exc:
            failures[ckpt] = f"{type(exc).__name__}: {exc}"
            print(f"  FAILED: {failures[ckpt]}", file=sys.stderr)
            traceback.print_exc(limit=2)
            _free_vram()
            continue

    # Tech: if every checkpoint failed, there's nothing to write — error out.
    # Why:  an empty per_window frame would make summarize() and the printout
    #       meaningless; a clear exit code 1 signals total failure.
    if not all_rows:
        print("ERROR: every checkpoint failed", file=sys.stderr)
        return 1

    # Tech: write the raw per-window rows and the aggregated per-checkpoint summary.
    # Why:  per_window.csv preserves the full detail for ad-hoc analysis; summary.csv
    #       is the headline table that aligns forecast quality with realized PnL.
    per_window = pd.DataFrame(all_rows)
    per_window_path = args.output_dir / "per_window.csv"
    per_window.to_csv(per_window_path, index=False)

    summary = summarize(per_window, load_times, backtest_summaries)
    summary_path = args.output_dir / "summary.csv"
    summary.to_csv(summary_path, index=False)

    # Tech: persist the invocation params plus any per-checkpoint failures.
    # Why:  records exactly what was run and which models didn't make it, so a sweep
    #       is reproducible and partial failures are documented, not silently dropped.
    write_params({**vars(args), "failures": failures}, args.output_dir / "params.json")

    # Tech: select the columns to display, appending the bt_* set unless backtests
    #       were skipped, and filter to those actually present.
    # Why:  the console table should show only relevant, existing columns — dropping
    #       backtest columns under --skip-backtest avoids a wall of n/a.
    print("\n=== Per-checkpoint summary ===")
    cols = [
        "checkpoint", "n_windows", "load_s", "mean_latency_s",
        "direction_hit_rate", "spearman_ic",
        "mean_abs_return_err", "mean_pred_return", "mean_real_return",
    ]
    if not args.skip_backtest:
        cols += [
            "bt_n_trades", "bt_net_pnl_points",
            "bt_sharpe_per_trade", "bt_max_drawdown_pts",
        ]
    cols = [c for c in cols if c in summary.columns]
    # Tech: per-column float formatters, with n/a fallbacks for NaN backtest cells.
    # Why:  fixed-width signed formats make the table columns line up and keep tiny
    #       returns legible; the pd.notna guards render missing backtest values as a
    #       tidy "n/a" instead of "nan".
    formatters = {
        "load_s":              lambda x: f"{x:7.1f}",
        "mean_latency_s":      lambda x: f"{x:7.3f}",
        "direction_hit_rate":  lambda x: f"{x:+.4f}",
        "spearman_ic":         lambda x: f"{x:+.4f}",
        "mean_abs_return_err": lambda x: f"{x:+.6f}",
        "mean_pred_return":    lambda x: f"{x:+.6f}",
        "mean_real_return":    lambda x: f"{x:+.6f}",
        "bt_net_pnl_points":   lambda x: f"{x:+.2f}" if pd.notna(x) else "  n/a",
        "bt_sharpe_per_trade": lambda x: f"{x:+.4f}" if pd.notna(x) else "  n/a",
        "bt_max_drawdown_pts": lambda x: f"{x:+.2f}" if pd.notna(x) else "  n/a",
    }
    print(summary[cols].to_string(
        index=False,
        formatters={k: v for k, v in formatters.items() if k in cols},
    ))

    # Tech: if any checkpoints failed, list them with their error messages.
    # Why:  surfacing failures at the end (not just mid-run) makes them impossible to
    #       miss when scanning the final output.
    if failures:
        print("\n=== Failures ===")
        for ckpt, msg in failures.items():
            print(f"  {ckpt}: {msg}")

    # Tech: print the paths of the written CSVs and each checkpoint's artifact dir.
    # Why:  a closing manifest of where everything landed saves hunting through the
    #       output tree afterward.
    print(f"\nwrote:")
    print(f"  {per_window_path}")
    print(f"  {summary_path}")
    if not args.skip_backtest:
        for ckpt, bt in backtest_summaries.items():
            print(f"  {bt['bt_output_dir']}/   ({ckpt})")
    return 0


if __name__ == "__main__":
    # Tech: run main() and propagate the exit code.
    # Why:  surfaces the sweep's success/total-failure status to the shell.
    sys.exit(main())
