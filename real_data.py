"""End-to-end backtest of Toto2Forecaster on the full TXF 1-min dataset.

Same shape as ``real_data_demo.py`` but uses the **whole** deduped 1-min
file by default, with a time-based split: the first 30 % of bars are
silent warmup (no trading), the trailing 70 % is the backtest window.

The per-forecast model context stays at ``context_length=3008`` bars —
Toto2 truncates anything longer, so scaling it with the dataset size
would not give the model more information.

Run from myPaper/:
    python -m forecast_eval.real_data

Quick sanity run on a subset:
    python -m forecast_eval.real_data --n-bars 20000 --stride-bars 60
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Tech: prepend the repo root so the absolute forecast_eval imports resolve when
#       the file is run directly.
# Why:  same trampoline as real_data_demo — supports both `python -m` and direct
#       execution from inside the folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from forecast_eval.forecaster.toto2 import Toto2Forecaster
from forecast_eval.logging_io import timestamped_run_dir, write_all
from forecast_eval.metrics import buy_and_hold_pnl, compute_metrics
from forecast_eval.run import run_backtest
from forecast_eval.strategy.threshold import ThresholdEmitter

DEFAULT_DATA = (
    Path(__file__).resolve().parent.parent
    / "dataset" / "tick" / "TXF_OHLC_1min.csv"
)


def load_minute_bars(path: Path, n_bars: int | None) -> pd.DataFrame:
    """Load the 1-min OHLC file, dropping NaN closes.

    If ``n_bars`` is None, return every traded minute. Otherwise return
    the trailing ``n_bars`` rows. See real_data_demo for why dropping
    NaNs matters (Toto2 propagates them; ~17 % of raw rows are gaps).
    """
    # Tech: read + parse + drop NaN closes; trim to the trailing n_bars only when a
    #       limit is given, then project to the tick schema.
    # Why:  this variant defaults to the *full* dataset (n_bars=None), unlike the
    #       demo — so the trim is conditional; everything else mirrors the demo loader.
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if n_bars is not None:
        df = df.tail(n_bars).reset_index(drop=True)
    return pd.DataFrame({
        "timestamp": df["timestamp"],
        "price": df["close"].astype(float),
        "volume": 1,
    })


def main(argv=None) -> int:
    # Tech: CLI like real_data_demo but adds --lookback-frac (warmup split) and
    #       defaults --n-bars to None (use everything).
    # Why:  this script's purpose is a full-history run with a principled train/eval
    #       split, so the warmup fraction is a first-class knob; context-length is
    #       capped by the model regardless of how much history is available.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help="Path to TXF_OHLC_1min.csv")
    ap.add_argument("--n-bars", type=int, default=None,
                    help="Trailing rows to use (default: full dataset)")
    ap.add_argument("--lookback-frac", type=float, default=0.30,
                    help="Fraction of bars used as silent warmup")
    ap.add_argument("--checkpoint", default="Datadog/Toto-2.0-313m",
                    help="HuggingFace toto2 checkpoint id")
    ap.add_argument("--context-length", type=int, default=3008,
                    help="Toto2 per-forecast context window (model-capped)")
    ap.add_argument("--horizon-bars", type=int, default=30)
    ap.add_argument("--stride-bars", type=int, default=30)
    ap.add_argument("--buy-threshold", type=float, default=0.0005)
    ap.add_argument("--sell-threshold", type=float, default=-0.0005)
    ap.add_argument("--aggression-ticks", type=int, default=0)
    ap.add_argument("--fee-rate", type=float, default=0.00015)
    ap.add_argument("--forced-close", action="store_true",
                    help="Force close at every DAY<->NIGHT session boundary")
    ap.add_argument("--output-dir", type=Path, default=None,
                    help="Defaults to <myPaper>/outputs/real_data_<timestamp>/")
    args = ap.parse_args(argv)

    # Tech: validate the lookback fraction is strictly inside (0, 1).
    # Why:  0 or 1 would leave either no warmup or no eval window; rejecting it early
    #       prevents a confusing downstream "warmup too small" or empty-eval error.
    if not 0.0 < args.lookback_frac < 1.0:
        print(f"ERROR: --lookback-frac must be in (0, 1), got {args.lookback_frac}",
              file=sys.stderr)
        return 2

    # Tech: default the output dir and verify the data file exists.
    # Why:  same guards as the demo — unique run dir, fail fast on a bad path.
    if args.output_dir is None:
        args.output_dir = timestamped_run_dir("real_data")

    if not args.data.exists():
        print(f"ERROR: data file not found at {args.data}", file=sys.stderr)
        return 2

    # Tech: load the bars and derive the warmup/eval split from lookback_frac.
    # Why:  the first `warmup` bars build the model's context silently (no trading);
    #       trading only happens over the trailing eval_bars, mirroring how a model
    #       deployed live would have history before it ever acts (SPEC §4.8).
    print(f"loading: {args.data}")
    ticks = load_minute_bars(args.data, args.n_bars)
    n = len(ticks)
    warmup = int(args.lookback_frac * n)
    eval_bars = n - warmup

    # Tech: refuse to run if the warmup slice is shorter than one model context.
    # Why:  Toto2 needs at least context_length bars before its first forecast; a
    #       warmup smaller than that would mean the model can never fire, so we stop
    #       with an actionable message instead of producing zero forecasts.
    if warmup < args.context_length:
        print(
            f"ERROR: warmup ({warmup} bars at lookback_frac={args.lookback_frac}) "
            f"is smaller than context_length ({args.context_length}). "
            f"Toto2 needs at least context_length bars before it can forecast.",
            file=sys.stderr,
        )
        return 2

    # Tech: print the full shape/range plus the explicit warmup and eval windows.
    # Why:  on a multi-hour full-dataset run it's important to see exactly which date
    #       ranges are warmup vs. evaluated before committing to it.
    print(f"  rows           : {n}")
    print(f"  range          : {ticks['timestamp'].iloc[0]}  ->  "
          f"{ticks['timestamp'].iloc[-1]}")
    print(f"  price range    : {ticks['price'].min():.1f} .. "
          f"{ticks['price'].max():.1f}")
    print(f"  warmup (30%)   : {warmup} bars  "
          f"[{ticks['timestamp'].iloc[0]} .. {ticks['timestamp'].iloc[warmup - 1]}]")
    print(f"  eval   (70%)   : {eval_bars} bars  "
          f"[{ticks['timestamp'].iloc[warmup]} .. {ticks['timestamp'].iloc[-1]}]")

    # Tech: build the forecaster with warmup_bars set to the *split* warmup (not the
    #       context length) and the threshold emitter.
    # Why:  here warmup is the data-driven 30% split, so trading is suppressed for the
    #       whole lookback region; context_length still caps each forecast's window.
    fc = Toto2Forecaster(
        warmup_bars=warmup,
        forecast_stride_bars=args.stride_bars,
        forecast_horizon_bars=args.horizon_bars,
        context_length=args.context_length,
        checkpoint=args.checkpoint,
        device="auto",
        bar_freq=None,
        signal_step="last",
    )
    em = ThresholdEmitter(
        buy_threshold=args.buy_threshold,
        sell_threshold=args.sell_threshold,
    )

    # Tech: print the expected forecast count over the eval window.
    # Why:  same cheap sanity check as the demo, computed against the split warmup.
    n_forecasts_expected = max(0, (n - warmup) // args.stride_bars + 1)
    print(f"  expect ~{n_forecasts_expected} forecasts "
          f"(warmup={warmup}, stride={args.stride_bars})")

    # Tech: run the backtest with a tqdm progress bar enabled, timing the whole pass.
    # Why:  the full dataset is millions of bars, so progress=True is worth the small
    #       overhead here (unlike the short demo) to show the run is advancing.
    t0 = time.time()
    res = run_backtest(
        ticks,
        forecaster=fc,
        emitter=em,
        tick_size=1.0,
        aggression_ticks=args.aggression_ticks,
        fee_rate=args.fee_rate,
        contract_multiplier=200.0,
        forced_close_on_session_end=args.forced_close,
        progress=True,
        progress_desc=f"backtest ({n} bars)",
    )
    elapsed = time.time() - t0
    print(f"  backtest done in {elapsed:.1f}s "
          f"({res.n_forecasts} forecasts, {res.n_orders} orders, "
          f"{len(res.fills)} fills)")

    # Tech: persist the artifact bundle (+ charts) and echo paths.
    # Why:  identical to the demo — full reproducibility via params.json and a price
    #       overlay from the source data.
    paths = write_all(res, args.output_dir, params=vars(args),
                      report_data_path=args.data)
    print(f"\nlogs written under {args.output_dir}/")
    for name, p in paths.items():
        print(f"  {name:10s} -> {p}")

    # Tech: compute and print the three metric blocks (trading/forecast/attribution).
    # Why:  same SPEC §7 deliverable and formatting convention as the demo.
    metrics = compute_metrics(res, ticks, forced_close=args.forced_close)

    print("\n=== Trading metrics ===")
    trading_keys = [k for k in metrics if k.startswith("trading")]
    for key in trading_keys:
        print(f"  [{key}]")
        for k, v in metrics[key].items():
            print(f"    {k:38s} {v if not isinstance(v, float) else f'{v:+.6f}'}")

    print("\n=== Forecast quality ===")
    for k, v in metrics["forecast"].items():
        print(f"  {k:38s} {v if not isinstance(v, float) else f'{v:+.6f}'}")

    print("\n=== Attribution (signal vs. realized) ===")
    for k, v in metrics["attribution"].items():
        print(f"  {k:38s} {v if not isinstance(v, float) else f'{v:+.6f}'}")

    # Buy-and-hold over the eval window only — comparing strategy PnL (which
    # only trades the trailing 70%) against buy-and-hold over the full 100%
    # would be apples-to-oranges.
    # Tech: slice the eval window and compute buy-and-hold on just those bars.
    # Why:  the strategy only traded the eval window, so the baseline must cover the
    #       same window for the comparison to be fair (a full-history hold would
    #       include the silent warmup the strategy never traded).
    eval_ticks = ticks.iloc[warmup:].reset_index(drop=True)
    print("\n=== Buy-and-hold floor (eval window, same fee) ===")
    bh = buy_and_hold_pnl(eval_ticks, fee_rate=args.fee_rate)
    for k, v in bh.items():
        print(f"  {k:38s} {v if not isinstance(v, float) else f'{v:+.6f}'}")

    # Tech: print strategy net, baseline net, and their difference.
    # Why:  the delta is the bottom line — positive means the model added value over
    #       passively holding the same window at the same cost.
    realized_net = res.portfolio.net_pnl()
    print(f"\nstrategy net (pts):     {realized_net:+.4f}")
    print(f"buy-and-hold net (pts): {bh['net_pnl_points']:+.4f}")
    print(f"strategy - buyhold:     {realized_net - bh['net_pnl_points']:+.4f}")

    return 0


if __name__ == "__main__":
    # Tech: run main() and propagate the exit code.
    # Why:  surfaces nonzero exits (bad path, bad split) to the shell.
    sys.exit(main())
