"""End-to-end backtest of Toto2Forecaster on real TXF 1-min bars.

Treats the 1-min OHLC close as the framework's "tick" price. The toto2
notebooks use context=3008 bars, horizon=30 bars; we match that.

Run from myPaper/:
    python -m forecast_eval.real_data_demo

Or with options:
    python -m forecast_eval.real_data_demo --n-bars 8000 \
        --checkpoint Datadog/Toto-2.0-313m \
        --buy-threshold 0.0005 --sell-threshold -0.0005
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow direct execution (`python real_data_demo.py` from inside the folder).
# Tech: prepend the repo root (parent of forecast_eval/) to sys.path before the
#       package imports below.
# Why:  the `forecast_eval.xxx` absolute imports need myPaper/ on the path; this
#       "trampoline" makes the file runnable directly, not only via `python -m`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from forecast_eval.forecaster.toto2 import Toto2Forecaster
from forecast_eval.logging_io import timestamped_run_dir, write_all
from forecast_eval.metrics import buy_and_hold_pnl, compute_metrics
from forecast_eval.run import run_backtest
from forecast_eval.strategy.threshold import ThresholdEmitter

# Tech: default location of the TXF 1-min file, resolved relative to this source.
# Why:  anchoring to the source tree (not CWD) means the default works regardless
#       of where the command is launched from.
DEFAULT_DATA = (
    Path(__file__).resolve().parent.parent
    / "dataset" / "tick" / "TXF_OHLC_1min.csv"
)


def load_minute_bars(path: Path, n_bars: int) -> pd.DataFrame:
    """Load the last `n_bars` non-NaN rows of the 1-min OHLC file.

    The raw 1-min file fills session gaps and halts with NaN close values
    (~17% of rows in the public TXF feed). Toto2 propagates NaN, so we
    drop them. After dropping, the framework still sees one row per
    *actual* traded minute — the "30-bar horizon" becomes "30 traded
    minutes," which is what the notebook setup intends.
    """
    # Tech: read the CSV, parse timestamps, drop NaN-close rows, keep the trailing
    #       n_bars, and project to the [timestamp, price, volume] tick schema.
    # Why:  dropping NaN closes is essential — Toto2 propagates NaN through the whole
    #       forecast; close becomes the framework's "price", and volume is a constant
    #       1 because the 1-min OHLC file carries no usable per-bar volume here.
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    df = df.tail(n_bars).reset_index(drop=True)
    return pd.DataFrame({
        "timestamp": df["timestamp"],
        "price": df["close"].astype(float),
        "volume": 1,
    })


def main(argv=None) -> int:
    # Tech: define the CLI — data path, window size, checkpoint, model context/
    #       horizon/stride, signal thresholds, execution knobs, and output dir.
    # Why:  every backtest parameter is exposed so runs are tunable without code
    #       edits; defaults match the toto2 notebook configuration (context 3008,
    #       horizon 30) so the demo reproduces that setup out of the box.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help="Path to TXF_OHLC_1min.csv")
    ap.add_argument("--n-bars", type=int, default=6000,
                    help="Trailing rows of the 1-min file to use")
    ap.add_argument("--checkpoint", default="Datadog/Toto-2.0-313m",
                    help="HuggingFace toto2 checkpoint id")
    ap.add_argument("--context-length", type=int, default=3008)
    ap.add_argument("--horizon-bars", type=int, default=30)
    ap.add_argument("--stride-bars", type=int, default=30)
    ap.add_argument("--buy-threshold", type=float, default=0.0005)
    ap.add_argument("--sell-threshold", type=float, default=-0.0005)
    ap.add_argument("--aggression-ticks", type=int, default=0)
    ap.add_argument("--fee-rate", type=float, default=0.00015)
    ap.add_argument("--forced-close", action="store_true",
                    help="Force close at every DAY<->NIGHT session boundary")
    ap.add_argument("--output-dir", type=Path, default=None,
                    help="Defaults to <myPaper>/outputs/real_data_demo_<timestamp>/")
    args = ap.parse_args(argv)

    # Tech: default the output dir to a timestamped run dir, and bail if the data
    #       file is missing.
    # Why:  a unique dir per run prevents clobbering; checking the file up front
    #       avoids loading a model only to fail on a bad path.
    if args.output_dir is None:
        args.output_dir = timestamped_run_dir("real_data_demo")

    if not args.data.exists():
        print(f"ERROR: data file not found at {args.data}", file=sys.stderr)
        return 2

    # Tech: load the bars and print a quick shape/range/price summary.
    # Why:  an immediate sanity echo catches an obviously wrong window (too few rows,
    #       unexpected dates) before the slow model load begins.
    print(f"loading: {args.data}")
    ticks = load_minute_bars(args.data, args.n_bars)
    print(f"  rows           : {len(ticks)}")
    print(f"  range          : {ticks['timestamp'].iloc[0]}  ->  "
          f"{ticks['timestamp'].iloc[-1]}")
    print(f"  price range    : {ticks['price'].min():.1f} .. "
          f"{ticks['price'].max():.1f}")

    # Tech: build the Toto2 forecaster (context==warmup) and the threshold emitter.
    # Why:  warmup_bars == context_length means the first forecast fires exactly when
    #       enough history exists; bar_freq=None because the data is already 1-min, so
    #       no resampling is needed.
    fc = Toto2Forecaster(
        warmup_bars=args.context_length,
        forecast_stride_bars=args.stride_bars,
        forecast_horizon_bars=args.horizon_bars,
        context_length=args.context_length,
        checkpoint=args.checkpoint,
        device="auto",
        bar_freq=None,         # data is already at 1-min resolution
        signal_step="last",
    )
    em = ThresholdEmitter(
        buy_threshold=args.buy_threshold,
        sell_threshold=args.sell_threshold,
    )

    # Tech: print the expected forecast count from window/warmup/stride arithmetic.
    # Why:  a cheap up-front expectation lets the user sanity-check that the run will
    #       actually produce forecasts (e.g. catches warmup > n_bars yielding zero).
    n_forecasts_expected = max(
        0, (len(ticks) - args.context_length) // args.stride_bars + 1,
    )
    print(f"  expect ~{n_forecasts_expected} forecasts "
          f"(warmup={args.context_length}, stride={args.stride_bars})")

    # Tech: run the full backtest, timing it, and print fills/orders/forecast counts.
    # Why:  this is the actual work; contract_multiplier=200 is the TXF point value,
    #       and the elapsed/counts line confirms the run did something before the
    #       (slower) metrics and logging steps.
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
    )
    elapsed = time.time() - t0
    print(f"  backtest done in {elapsed:.1f}s "
          f"({res.n_forecasts} forecasts, {res.n_orders} orders, "
          f"{len(res.fills)} fills)")

    # Tech: write the full artifact bundle (+ auto charts) and echo the paths.
    # Why:  vars(args) persists every CLI knob as params.json for reproducibility;
    #       passing the data path lets the price chart use the real series as background.
    paths = write_all(res, args.output_dir, params=vars(args),
                      report_data_path=args.data)
    print(f"\nlogs written under {args.output_dir}/")
    for name, p in paths.items():
        print(f"  {name:10s} -> {p}")

    # Tech: compute the metric pack and print trading (per-session if forced-close),
    #       forecast-quality, and attribution blocks.
    # Why:  these three blocks are the SPEC §7 deliverable; floats are formatted to 6
    #       decimals so small returns stay visible, while non-floats print as-is.
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

    # Tech: compute a buy-and-hold baseline on the same window/fee and print it,
    #       then the strategy-minus-baseline delta.
    # Why:  net PnL is meaningless without a reference — beating buy-and-hold (the
    #       "floor", SPEC §6 Phase 5) under identical costs is the bar the model must
    #       clear to justify trading at all.
    print("\n=== Buy-and-hold floor (same window, same fee) ===")
    bh = buy_and_hold_pnl(ticks, fee_rate=args.fee_rate)
    for k, v in bh.items():
        print(f"  {k:38s} {v if not isinstance(v, float) else f'{v:+.6f}'}")

    realized_net = res.portfolio.net_pnl()
    print(f"\nstrategy net (pts):     {realized_net:+.4f}")
    print(f"buy-and-hold net (pts): {bh['net_pnl_points']:+.4f}")
    print(f"strategy - buyhold:     {realized_net - bh['net_pnl_points']:+.4f}")

    return 0


if __name__ == "__main__":
    # Tech: run main() and propagate its exit code.
    # Why:  makes failures (missing data, etc.) visible to the shell for scripting.
    sys.exit(main())
