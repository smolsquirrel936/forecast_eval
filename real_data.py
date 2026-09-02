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

from forecast_eval.environment import Environment
from forecast_eval.forecaster.toto2 import Toto2Forecaster
from forecast_eval.logging_io import timestamped_run_dir, write_all
from forecast_eval.metrics import buy_and_hold_pnl, compute_metrics
from forecast_eval.run import run_backtest
from forecast_eval.strategy import wiring as strategy_wiring
from forecast_eval.strategy.threshold import ThresholdEmitter

# Tech: the literal defaults these twelve flags carried before `--strategy`
#       existed, keyed by argparse destination. The flags themselves now use
#       `argparse.SUPPRESS`, and this table is applied when no `--strategy` is
#       given.
# Why:  STRATEGY_SPLIT_PLAN §2.2 -- every command string recorded in
#       `results.tsv` must keep reproducing byte-identically, so the old default
#       for every knob has to survive somewhere. Moving them here rather than
#       deleting them makes "what a bare run does" one readable table instead of
#       twelve scattered literals, and lets the resolver below tell a typed flag
#       from an untyped one, which is what a literal default cannot express.
CLI_STRATEGY_DEFAULTS = {
    "checkpoint": "Datadog/Toto-2.0-313m",
    "context_length": 3008,
    "horizon_bars": 30,
    "stride_bars": 30,
    "buy_threshold": 0.0005,
    "sell_threshold": -0.0005,
    "min_prob": None,
    "full_confidence_prob": 0.9,
    "aggression_ticks": 0,
    "max_aggression_ticks": None,
    "max_position": 1,
    "forced_close": False,
}


def _resolve_strategy_knobs(args) -> dict:
    # Tech: fill in every knob in CLI_STRATEGY_DEFAULTS on `args` -- from the
    #       named strategy package when `--strategy` was given, otherwise from
    #       this CLI's own defaults -- with any explicitly typed flag winning.
    #       Returns {knob: origin} for the banner.
    # Why:  writing the resolved values back onto `args` is what keeps the rest of
    #       this function, and `params=vars(args)` in the artifact bundle,
    #       unchanged: every downstream `args.stride_bars` still reads the value
    #       that actually ran, and `params.json` records the resolved set rather
    #       than a half-empty namespace.
    explicit = {k: v for k, v in vars(args).items() if k in CLI_STRATEGY_DEFAULTS}
    if args.strategy is None:
        base, source = dict(CLI_STRATEGY_DEFAULTS), "cli-default"
    else:
        base, source = strategy_wiring.params(args.strategy), args.strategy
        # Tech: the package's knob set must be exactly this CLI's knob set.
        # Why:  a package that adds a knob this CLI cannot pass would be tuned in
        #       research and silently dropped at evaluation; one that drops a knob
        #       would fall back to a stale literal. Either way the recorded row
        #       and the run disagree, which is the failure `live/config.py` exists
        #       to prevent -- so it fails here instead, at import-time cost zero.
        if set(base) != set(CLI_STRATEGY_DEFAULTS):
            raise ValueError(
                f"strategy {args.strategy!r} declares knobs "
                f"{sorted(base)}, but this CLI resolves "
                f"{sorted(CLI_STRATEGY_DEFAULTS)}"
            )
    origins = {}
    for knob, value in base.items():
        if knob in explicit:
            origins[knob] = "flag"
        else:
            origins[knob] = source
            setattr(args, knob, value)
    return origins


def load_minute_bars(path: Path, n_bars: int | None) -> pd.DataFrame:
    """Load a 1-min OHLC file as ticks, dropping NaN closes.

    Handles two schemas transparently (same as real_data_demo):

    * ``tick/TXF_OHLC_1min.csv`` — columns ``timestamp,open,high,low,close``.
    * ``RPT/TXF_RPT_minute.csv`` — columns
      ``datetime,contract,open,high,low,close,volume``. TXF trades several
      contract months at once, so a minute *could* appear under multiple
      ``contract`` values; we keep the **highest-volume** row per minute.
      **MEASURED 2026-08-23, re-verified 2026-09-01: in practice it never
      does** — all three RPT files carry exactly one contract per minute, so
      this collapse is a no-op and the files are already
      front-month-continuous. Since TAPE_REBUILD_PLAN T2 the splice is
      ``live.roll.roll_at(month, policy)`` — the instant each deployment
      actually rolls, 12:45 for day and combined, 15:30 for night. It used to
      be one calendar day *after* each expiry (SHIOAJI_PLAN §2.6), which no
      deployment could execute; pre-rebuild copies are in
      ``dataset/RPT/_prerebuild_backup_20260901/``.

    If ``n_bars`` is None, return every traded minute; otherwise return
    the trailing ``n_bars`` rows. Dropping NaN closes matters because
    Toto2 propagates them (~17 % of raw rows are gaps).
    """
    # Tech: read the CSV and normalize the timestamp column name — the RPT file
    #       calls it "datetime", the legacy tick file calls it "timestamp".
    # Why:  downstream code and the framework's tick schema key off "timestamp";
    #       normalizing here lets one loader serve both files unchanged.
    df = pd.read_csv(path)
    if "datetime" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"datetime": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Tech: if a per-minute "contract" column exists, collapse each minute to the
    #       single highest-volume contract before anything else.
    # Why:  a defence against several rows landing on one timestamp, which would
    #       break the monotonic-time assumption and splice prices across months.
    #       **It has never had to fire.** MEASURED 2026-08-23 across all three RPT
    #       files (1.1M / 1.7M / 2.9M rows): zero minutes carry more than one
    #       contract, so the series arrives already front-month-continuous. That
    #       matters beyond trivia — the live splice was built to reproduce "the
    #       evaluated convention", and the evaluated convention turned out to be a
    #       *calendar*, not this rule (SHIOAJI_PLAN §2.6). Kept as a guard, not
    #       relied on as a definition.
    if "contract" in df.columns:
        idx = df.groupby("timestamp")["volume"].idxmax()
        df = df.loc[idx].reset_index(drop=True)

    # Tech: drop NaN closes, sort by time, trim to the trailing n_bars only when a
    #       limit is given, then project to the [timestamp, price, volume] schema.
    # Why:  this variant defaults to the *full* dataset (n_bars=None), unlike the
    #       demo — so the trim is conditional; real per-bar volume is used when
    #       present, else a constant 1 for the volume-less legacy file.
    df = df.dropna(subset=["close"]).sort_values("timestamp").reset_index(drop=True)
    if n_bars is not None:
        df = df.tail(n_bars).reset_index(drop=True)
    return pd.DataFrame({
        "timestamp": df["timestamp"],
        "price": df["close"].astype(float),
        "volume": df["volume"].astype(float) if "volume" in df.columns else 1,
    })


def main(argv=None) -> int:
    # Tech: CLI like real_data_demo but adds --lookback-frac (warmup split) and
    #       defaults --n-bars to None (use everything).
    # Why:  this script's purpose is a full-history run with a principled train/eval
    #       split, so the warmup fraction is a first-class knob; context-length is
    #       capped by the model regardless of how much history is available.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Tech: --data is required; this CLI has no default dataset path.
    # Why:  the removed default named the DAY file while every tuning run in
    #       `results.tsv` used the NIGHT one — a session chosen by a default that
    #       nothing can check. `fetch_bars` dropped its own session default for
    #       exactly this reason (SHIOAJI_PLAN §2.12): a wrong one is undetectable
    #       once it ships, so this CLI names the file or refuses to run.
    ap.add_argument("--data", type=Path, required=True,
                    help="Path to a 1-min OHLC csv (RPT or tick schema)")
    ap.add_argument("--n-bars", type=int, default=None,
                    help="Trailing rows to use (default: full dataset)")
    ap.add_argument("--lookback-frac", type=float, default=0.30,
                    help="Fraction of bars used as silent warmup")
    # Tech: --warmup-bars names the split in bars and, when given, overrides
    #       --lookback-frac entirely.
    # Why:  DAY_SESSION_PLAN 2.2, MEASURED — a fraction is not reproducible across
    #       tapes of different length. The 2026-08-24 re-baseline re-ran the pinned
    #       config on a grown tape at the same --lookback-frac 0.30 and the split
    #       moved 520004 -> 536373 bars; forecasts fire at `warmup + k*stride`, and
    #       16369 mod 30 = 19, so every forecast landed 19 bars off the original
    #       grid (0 shared timestamps out of 40443 old / 41716 new). Same strategy,
    #       same data, -10100.62 points over the identical calendar window --
    #       Calmar 13.6742 -> 8.7651. Recovering the original run needed a
    #       10-decimal fraction reverse-engineered from the row count. A bar count
    #       is the number that actually identifies the split, so it is the one a
    #       `results.tsv` row pins (the `warmup_bars` column) and the one a re-run
    #       passes back in.
    ap.add_argument("--warmup-bars", type=int, default=None,
                    help="Silent-warmup length in BARS; overrides --lookback-frac. "
                         "Reproducible across tapes of different length, which a "
                         "fraction is not -- prefer this for any run that will be "
                         "recorded in results.tsv")
    # Tech: --strategy raises one of the three per-session packages
    #       (STRATEGY_SPLIT_PLAN S3); every knob below it also declares then
    #       defaults from that package instead of from this CLI.
    # Why:  the tuned configuration lived only as a flag string in `results.tsv`,
    #       so reproducing a run meant retyping six numbers correctly and a wrong
    #       one was undetectable. Naming the deployment makes the whole set travel
    #       together, exactly as `live/cli.py --config` does for a live run. It
    #       defaults to None so every recorded flag-string command still runs, and
    #       runs identically.
    ap.add_argument("--strategy", choices=list(strategy_wiring.SESSIONS),
                    default=None,
                    help="Session strategy package to run; supplies defaults for "
                         "the knobs below. Omit for this CLI's own defaults.")
    # Tech: every knob a strategy package also declares uses SUPPRESS rather than
    #       a literal default, and the resolved value is written back onto `args`
    #       immediately after parsing (see _resolve_strategy_knobs).
    # Why:  SUPPRESS is what makes "the user typed this flag" observable at all --
    #       with a literal default, a flag passed at its default value is
    #       indistinguishable from one never passed, and `--strategy night
    #       --buy-threshold 0.0005` would silently run 0.00045. The values that
    #       were these defaults now live in CLI_STRATEGY_DEFAULTS, and the
    #       no---strategy path applies them unchanged.
    ap.add_argument("--checkpoint", default=argparse.SUPPRESS,
                    help="HuggingFace toto2 checkpoint id")
    ap.add_argument("--context-length", type=int, default=argparse.SUPPRESS,
                    help="Toto2 per-forecast context window (model-capped)")
    ap.add_argument("--horizon-bars", type=int, default=argparse.SUPPRESS)
    ap.add_argument("--stride-bars", type=int, default=argparse.SUPPRESS)
    ap.add_argument("--buy-threshold", type=float, default=argparse.SUPPRESS)
    ap.add_argument("--sell-threshold", type=float, default=argparse.SUPPRESS)
    ap.add_argument("--aggression-ticks", type=int, default=argparse.SUPPRESS)
    # Confidence-aware order placement (ORDER_PLACEMENT_IDEAS #1/#2/#7). All default
    # to OFF so a plain run reproduces the prior behavior exactly.
    ap.add_argument("--min-prob", type=float, default=argparse.SUPPRESS,
                    help="#2: gate trades on Toto2 directional probability in "
                         "[0.5,1); HOLD when below. Off by default.")
    ap.add_argument("--full-confidence-prob", type=float,
                    default=argparse.SUPPRESS,
                    help="Directional probability at which conviction strength "
                         "saturates to 1.0 (drives #1/#7).")
    ap.add_argument("--max-aggression-ticks", type=int,
                    default=argparse.SUPPRESS,
                    help="#1: aggressive end of a confidence-scaled limit offset "
                         "(base = --aggression-ticks). Off (no scaling) by default.")
    ap.add_argument("--max-position", type=int, default=argparse.SUPPRESS,
                    help="#7: max contracts for a full-conviction entry; size scales "
                         "with strength up to this. 1 disables sizing.")
    ap.add_argument("--fee-rate", type=float, default=0.00015)
    ap.add_argument("--forced-close", action="store_true",
                    default=argparse.SUPPRESS,
                    help="Force close at every DAY<->NIGHT session boundary")
    ap.add_argument("--precompute", action="store_true",
                    help="Batch all forecasts on the GPU before replaying execution "
                         "(much faster for Toto2; identical results)")
    ap.add_argument("--batch-size", default="auto",
                    help="Forecast batch size for --precompute: 'auto' (probe VRAM) "
                         "or an integer")
    ap.add_argument("--output-dir", type=Path, default=None,
                    help="Defaults to <myPaper>/outputs/real_data_<timestamp>/")
    args = ap.parse_args(argv)

    # Tech: resolve the strategy-owned knobs onto `args` before anything reads
    #       them, and print the resolution when a package was named.
    # Why:  §2.2 -- "which knob came from where" must be visible in the run's own
    #       output, because a pinned deployment and a hand-typed override now look
    #       identical once they reach the model. Nothing is printed on the bare
    #       path so an existing recorded command's output is unchanged.
    origins = _resolve_strategy_knobs(args)
    if args.strategy is not None:
        # Tech: stamp the package's forecaster hash onto `args`, so it reaches
        #       the banner and `params.json` without a second lookup.
        # Why:  §2.4 — this is the column a `results.tsv` row needs to say which
        #       forecast stream it was scored on, and a number nobody can find is
        #       a number nobody records. Printing it beside the knobs makes it
        #       copyable straight out of the run that produced the result.
        args.forecaster_sha = strategy_wiring.forecaster_sha(args.strategy)
        print(f"strategy: {args.strategy} "
              f"(forecaster_sha {args.forecaster_sha})")
        for knob in sorted(origins):
            print(f"  {knob:22} = {getattr(args, knob)!r:24} "
                  f"[{origins[knob]}]")

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

    # Tech: load the bars and derive the warmup/eval split -- from --warmup-bars
    #       when it was given, otherwise from --lookback-frac.
    # Why:  the first `warmup` bars build the model's context silently (no trading);
    #       trading only happens over the trailing eval_bars, mirroring how a model
    #       deployed live would have history before it ever acts (SPEC §4.8).
    print(f"loading: {args.data}")
    ticks = load_minute_bars(args.data, args.n_bars)
    n = len(ticks)

    # Tech: the timestamps of the bars that actually reach the run loop --
    #       `Environment` drops everything outside trading hours.
    # Why:  MEASURED 2026-09-01. This is the unit `warmup` is denominated in:
    #       `run_backtest` fires its first forecast at `bar_idx >= warmup` where
    #       `bar_idx` counts only in-session bars, and `precompute_forecasts`
    #       keys its cache the same way. The loaded frame is NOT that sequence.
    #       The day tape carries 1215 rows stamped 13:45 -- outside the DAY
    #       session by both `policy.label` and `classify_session`, median volume
    #       4 against 2191 on the 13:44 bar -- and the night tape carries 82
    #       such rows. So a warmup quoted against raw row positions names a
    #       different bar than the split actually lands on: 904 rows different
    #       on the day tape, about three sessions. Everything below is computed
    #       on this sequence so the banner cannot disagree with the run.
    in_session = [m.timestamp for m in Environment(ticks).stream()]
    n_session = len(in_session)

    warmup = (args.warmup_bars if args.warmup_bars is not None
              else int(args.lookback_frac * n))
    eval_bars = n_session - warmup

    # Tech: an explicit --warmup-bars must leave a non-empty eval window, bounded
    #       by the in-session count rather than the raw row count.
    # Why:  --lookback-frac was validated into (0, 1) above, which bounded the
    #       derived split implicitly; a bar count carries no such bound. The
    #       bound is `n_session` because that is what the index means -- a value
    #       between `n_session` and `n` would pass a raw-row check and still
    #       evaluate nothing.
    if args.warmup_bars is not None and not 0 < args.warmup_bars < n_session:
        print(f"ERROR: --warmup-bars must be in (0, {n_session}) -- the count of "
              f"IN-SESSION bars, not the {n} rows loaded -- got "
              f"{args.warmup_bars}", file=sys.stderr)
        return 2

    # Tech: refuse to run if the warmup slice is shorter than one model context.
    # Why:  Toto2 needs at least context_length bars before its first forecast; a
    #       warmup smaller than that would mean the model can never fire, so we stop
    #       with an actionable message instead of producing zero forecasts.
    if warmup < args.context_length:
        print(
            f"ERROR: warmup ({warmup} bars from "
            f"{'--warmup-bars' if args.warmup_bars is not None else f'lookback_frac={args.lookback_frac}'}) "
            f"is smaller than context_length ({args.context_length}). "
            f"Toto2 needs at least context_length bars before it can forecast.",
            file=sys.stderr,
        )
        return 2

    # Tech: print the full shape/range plus the explicit warmup and eval windows.
    # Why:  on a multi-hour full-dataset run it's important to see exactly which date
    #       ranges are warmup vs. evaluated before committing to it.
    print(f"  rows loaded    : {n}")
    print(f"  in-session bars: {n_session}"
          + (f"   ({n - n_session} rows dropped as off-session)"
             if n != n_session else ""))
    print(f"  range          : {ticks['timestamp'].iloc[0]}  ->  "
          f"{ticks['timestamp'].iloc[-1]}")
    print(f"  price range    : {ticks['price'].min():.1f} .. "
          f"{ticks['price'].max():.1f}")
    print(f"  warmup ({warmup / n_session:5.1%}) : {warmup} bars  "
          f"[{in_session[0]} .. {in_session[warmup - 1]}]")
    print(f"  eval   ({eval_bars / n_session:5.1%}) : {eval_bars} bars  "
          f"[{in_session[warmup]} .. {in_session[-1]}]")
    # Tech: say so out loud when the fraction path was used on a tape carrying
    #       off-session rows.
    # Why:  --lookback-frac multiplies the RAW row count, so on such a tape the
    #       split it names is not the fraction it reads as. The arithmetic is
    #       left alone deliberately -- changing it would silently move every run
    #       ever recorded against a fraction, which is the failure mode 2.2 is
    #       about. Saying it is the honest middle: use --warmup-bars for anything
    #       that will be pinned.
    if args.warmup_bars is None and n != n_session:
        print(f"  NOTE           : --lookback-frac {args.lookback_frac} was taken "
              f"over {n} RAW rows, not the {n_session} in-session bars the split "
              f"is measured in; prefer --warmup-bars for a recorded run")

    # Tech: build the forecaster with warmup_bars set to the *split* warmup (not the
    #       context length) and the threshold emitter.
    # Why:  here warmup is the data-driven 30% split, so trading is suppressed for the
    #       whole lookback region; context_length still caps each forecast's window.
    # Tech: with `--strategy`, the three components come from that package; the
    #       Trader arrives as a factory over Execution and any exit rule with it.
    #       Without it, the original construction runs unchanged.
    # Why:  §2.2/§2.1. This is the whole selector: one branch, and the `None` side
    #       is the code that was here before, so a recorded flag-string command is
    #       byte-identical. The resolved knobs are passed through explicitly
    #       rather than re-read inside the package, so an explicit CLI flag still
    #       beats the package's own default.
    if args.strategy is None:
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
            min_prob=args.min_prob,
            full_confidence_prob=args.full_confidence_prob,
        )
        make_trader = None
        exit_rule = None
    else:
        stack = strategy_wiring.make_strategy(
            args.strategy,
            warmup_bars=warmup,
            tick_size=1.0,
            **{knob: getattr(args, knob) for knob in CLI_STRATEGY_DEFAULTS},
        )
        fc = stack["forecaster"]
        em = stack["emitter"]
        make_trader = stack["make_trader"]
        exit_rule = stack["exit_rule"]

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
        make_trader=make_trader,
        exit_rule=exit_rule,
        tick_size=1.0,
        aggression_ticks=args.aggression_ticks,
        max_aggression_ticks=args.max_aggression_ticks,
        max_position=args.max_position,
        fee_rate=args.fee_rate,
        contract_multiplier=200.0,
        forced_close_on_session_end=args.forced_close,
        progress=True,
        progress_desc=f"backtest ({n} bars)",
        # Only materialize the trailing context the forecaster consumes, not the
        # whole history-through-t, each stride. Safe here because bar_freq=None, so
        # context_length raw rows == context_length bars (identical model input).
        # If a bar_freq aggregation were enabled, this would need a larger window.
        history_window=args.context_length,
        # Batch every forecast on the GPU up front, then replay execution. Only
        # used when --precompute is passed; batch_size 'auto' probes free VRAM.
        precompute=args.precompute,
        batch_size=(args.batch_size if args.batch_size == "auto"
                    else int(args.batch_size)),
    )
    elapsed = time.time() - t0
    print(f"  backtest done in {elapsed:.1f}s "
          f"({res.n_forecasts} forecasts, {res.n_orders} orders, "
          f"{len(res.fills)} fills)")

    # Tech: compute the metric pack and the buy-and-hold floor up front, before the
    #       artifact/report write.
    # Why:  write_all auto-generates the report, and the report now opens with these
    #       numbers (Settings + Result summary) — so they must exist before it runs.
    #       The same dicts are reused for the terminal blocks below (no recompute).
    metrics = compute_metrics(res, ticks, forced_close=args.forced_close)
    bh = buy_and_hold_pnl(ticks, fee_rate=args.fee_rate)

    # Tech: persist the artifact bundle (+ charts) and echo paths.
    # Why:  identical to the demo — full reproducibility via params.json and a price
    #       overlay from the source data; metrics/buy_and_hold feed the report tables.
    paths = write_all(res, args.output_dir, params=vars(args),
                      report_data_path=args.data, report_ticks=ticks,
                      metrics=metrics, buy_and_hold=bh, ext=".csv")
    print(f"\nlogs written under {args.output_dir}/")
    for name, p in paths.items():
        print(f"  {name:10s} -> {p}")

    # Tech: print the three metric blocks (trading/forecast/attribution).
    # Why:  same SPEC §7 deliverable and formatting convention as the demo.
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

    # Tech: print the buy-and-hold floor (computed above over the *full* loaded
    #       series, warmup + eval).
    # Why:  this is the whole-dataset floor — what a passive holder would have earned
    #       over the entire window at the same fee. Note the strategy only trades the
    #       trailing eval region, so the warmup bars here are price drift the strategy
    #       never participated in; the delta below is therefore not a like-for-like
    #       window comparison (it spans more than the strategy traded).
    print("\n=== Buy-and-hold floor (full data, same fee) ===")
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
