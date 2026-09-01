"""DAY_SESSION_PLAN W5b / TAPE_REBUILD_PLAN T7 — the band × min-prob search.

One GPU precompute, then every cell replayed off the cached forecasts. **The
emitter never changes the forecasts** — it reads a `Forecast` and returns a
direction — so a sweep over emitter settings does not need the model again. That
turns ~4 minutes a candidate into ~2 seconds, which is what makes a 30-cell grid
and its out-of-sample split affordable at all.

**Selection is on the WORSE half, not the full window.** W5b found that ranking
by full-window Calmar picks a fiction: `band=0.004` scored 14.7697 on the
pre-rebuild day tape and reads as "day nearly matches night", but it is 27 trades
with 10 in the out-of-sample half and a first-half Calmar of 2.2781 — the
degenerate trade-almost-never corner. Across that grid `spearman(h1, h2) = 0.45`,
so full-window rank is weak evidence on this tape. This module therefore reports
both halves for every cell and sorts on the worse one.

W5b's own harness was ad hoc and did not survive as a file, which is why T7 could
not simply re-run it. This is that harness, written down.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time as _time
from pathlib import Path

import pandas as pd

from forecast_eval.environment import Environment
from forecast_eval.metrics import build_trades
from forecast_eval.real_data import load_minute_bars
from forecast_eval.run import precompute_forecasts, run_backtest
from forecast_eval.strategy.threshold import ThresholdEmitter

# Tech: the grid. Bands in return units, probability gates in [0.5, 1).
# Why:  W5b's exact grid was never recorded — only five of its cells and the
#       count ("33-cell"). This one contains every reported cell (0.00045/0.50,
#       0.001/0.65, 0.00125/0.65, 0.0015/0.50, 0.003/0.65, 0.004/0.50) so the
#       re-measurement can be compared to what was written down, and spreads the
#       rest around the ridge W5b identified at band 0.001–0.0015. Stated as data
#       here rather than passed on a command line so a re-run cannot quietly use
#       a different grid than the one a recorded result came from.
BANDS = (0.00045, 0.00075, 0.001, 0.00125, 0.0015, 0.002,
         0.0025, 0.003, 0.0035, 0.004, 0.005)
MIN_PROBS = (0.5, 0.65, 0.8)

# Tech: the date the eval window is cut into two halves for scoring.
# Why:  DAY_SESSION_PLAN W5b's split, kept identical so the re-measured table can
#       be laid beside the provisional one. It is a calendar date rather than a
#       median trade index so the two halves stay the same two market regimes no
#       matter how many trades a cell takes.
HALF_SPLIT = pd.Timestamp("2023-06-01")


def as_frame(trades) -> pd.DataFrame:
    # Tech: `build_trades` returns a list of `TradeRecord` dataclasses.
    # Why:  the sweep slices by entry timestamp and sums a column, both of which
    #       want a frame — and `entry_ts` must be a real Timestamp for the half
    #       split to compare against `HALF_SPLIT` rather than raise.
    if not trades:
        return pd.DataFrame(columns=["entry_ts", "net_pnl_points"])
    frame = pd.DataFrame([dataclasses.asdict(t) for t in trades])
    frame["entry_ts"] = pd.to_datetime(frame["entry_ts"])
    return frame


def calmar(trades: pd.DataFrame) -> tuple[float, float, int]:
    """`(calmar, net, n)` over a set of round-trips; Calmar is net / |max dd|."""
    # Tech: equity is the cumulative per-trade net PnL; drawdown its running peak.
    # Why:  the same net/|dd| the `results.tsv` rows quote, computed on trades
    #       rather than bars — a per-trade equity curve is what `build_trades`
    #       produces and what the recorded Calmars were derived from.
    if trades.empty:
        return 0.0, 0.0, 0
    net = float(trades["net_pnl_points"].sum())
    equity = trades["net_pnl_points"].cumsum()
    dd = float((equity - equity.cummax()).min())
    return (net / abs(dd) if dd < 0 else float("inf")), net, len(trades)


def sweep(ticks, forecaster, cache, *, fee_rate: float) -> pd.DataFrame:
    rows = []
    for band in BANDS:
        for min_prob in MIN_PROBS:
            # Tech: a fresh emitter for every cell.
            # Why:  `ThresholdEmitter` carries rolling deques; reusing one across
            #       cells silently produces wrong numbers, which is the same trap
            #       `fee_sensitivity_sweep`'s factory arguments exist to prevent.
            res = run_backtest(
                ticks,
                forecaster=forecaster,
                emitter=ThresholdEmitter(buy_threshold=band,
                                         sell_threshold=-band,
                                         min_prob=min_prob),
                tick_size=1.0, aggression_ticks=0, max_position=1,
                fee_rate=fee_rate, contract_multiplier=200.0,
                forced_close_on_session_end=False,
                history_window=forecaster.context_length,
                forecasts_cache=cache,
            )
            trades = as_frame(build_trades(res.fills))
            full, net, n = calmar(trades)
            if n:
                first = trades["entry_ts"] < HALF_SPLIT
                h1, _, n1 = calmar(trades[first])
                h2, _, n2 = calmar(trades[~first].reset_index(drop=True))
            else:
                h1 = h2 = 0.0
                n1 = n2 = 0
            rows.append(dict(band=band, min_prob=min_prob, n=n, n_h1=n1, n_h2=n2,
                             h1=h1, h2=h2, worst=min(h1, h2), full=full, net=net))
            print(f"  band={band:<8g} p={min_prob:<5g} n={n:>4}  "
                  f"h1={h1:>8.4f} h2={h2:>8.4f} worst={min(h1, h2):>8.4f} "
                  f"full={full:>8.4f}", flush=True)
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--warmup-bars", type=int, required=True,
                    help="RECOMPUTE this from a target timestamp after any tape "
                         "rebuild -- a bar count is an index, and inserting bars "
                         "before the split moves what it names")
    ap.add_argument("--checkpoint", default="Datadog/Toto-2.0-1B")
    ap.add_argument("--context-length", type=int, default=3008)
    ap.add_argument("--horizon-bars", type=int, default=30)
    ap.add_argument("--stride-bars", type=int, default=30)
    ap.add_argument("--fee-rate", type=float, default=0.00015)
    ap.add_argument("--batch-size", default="auto")
    ap.add_argument("--out", type=Path, default=None, help="write the table as CSV")
    args = ap.parse_args(argv)

    from forecast_eval.forecaster.toto2 import Toto2Forecaster

    t0 = _time.time()
    ticks = load_minute_bars(args.data, None)
    in_session = [m.timestamp for m in Environment(ticks).stream()]
    if not 0 < args.warmup_bars < len(in_session):
        print(f"ERROR: --warmup-bars must be in (0, {len(in_session)})",
              file=sys.stderr)
        return 2
    print(f"{args.data}\n  {len(ticks):,} rows, {len(in_session):,} in-session")
    print(f"  warmup {args.warmup_bars:,} -> eval "
          f"[{in_session[args.warmup_bars]} .. {in_session[-1]}], "
          f"{len(in_session) - args.warmup_bars:,} bars")
    print(f"  half split {HALF_SPLIT.date()}   grid "
          f"{len(BANDS)}x{len(MIN_PROBS)} = {len(BANDS) * len(MIN_PROBS)} cells",
          flush=True)

    fc = Toto2Forecaster(
        warmup_bars=args.warmup_bars, forecast_stride_bars=args.stride_bars,
        forecast_horizon_bars=args.horizon_bars,
        context_length=args.context_length, checkpoint=args.checkpoint,
        device="auto", bar_freq=None, signal_step="last")
    cache = precompute_forecasts(
        fc, ticks,
        batch_size=(args.batch_size if args.batch_size == "auto"
                    else int(args.batch_size)),
        progress=True)
    print(f"  precomputed {len(cache):,} forecasts in {_time.time() - t0:.0f}s",
          flush=True)

    table = sweep(ticks, fc, cache, fee_rate=args.fee_rate)

    # Tech: how well full-window rank predicts either half, and h1 predicts h2.
    # Why:  W5b's reason for scoring on halves at all. A low correlation here is
    #       the evidence that the full-window ranking cannot be trusted on this
    #       tape; quoting the selection without it leaves the rule unmotivated.
    scored = table[table["n"] > 0]
    print(f"\nrank agreement across {len(scored)} non-empty cells: "
          f"spearman(h1,h2) = {scored['h1'].corr(scored['h2'], method='spearman'):.2f}, "
          f"pearson = {scored['h1'].corr(scored['h2']):.2f}")

    print("\ntop 8 by WORST half (the selection rule):")
    print(table.sort_values("worst", ascending=False).head(8)
          .to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print("\ntop 5 by FULL window (the rule W5b rejected):")
    print(table.sort_values("full", ascending=False).head(5)
          .to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    if args.out:
        table.to_csv(args.out, index=False)
        print(f"\nwrote {args.out}")
    print(f"total {_time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
