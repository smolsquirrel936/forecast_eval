"""How quickly a backtest gets off the wrong side of a trend reversal.

Calmar says how much a strategy made per unit of drawdown; it does not say *why*
the drawdown happened. For fullday the measured why is reaction speed: after a
turn it keeps the old position for a median ~32 h and hands back ~half its best
open profit (2026-10-26 analysis). This tool turns that into one number a
keep/discard loop can rank on, next to Calmar.

**The reversals come from the tape, not from the strategy.** A percent zigzag
over the minute closes marks every swing of at least ``swing`` (default 3 %).
The swings are a function of the tape alone, so every run on the same tape is
scored against the *same* reversals — across strides, gates and stops — and
two runs can be compared leg by leg.

**Reversal ride (the primary score, lower = quicker).** For each swing leg, the
points the strategy lost while positioned *against* the leg, divided by the
leg's size: 0 = never against it, 1 = rode the whole leg the wrong way. It
counts both halves of a slow reaction — holding the old position through the
turn, and entering against the new leg part-way down it. The run's score is
the mean over legs.

    uv run --no-sync python -m forecast_eval.tools.reversal_reaction outputs/<run>
    uv run --no-sync python -m forecast_eval.tools.reversal_reaction outputs/<run> --baseline outputs/<incumbent>
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

SWING = 0.03
BOOTSTRAP = 2000
SEED = 20261026


def zigzag(prices: np.ndarray, swing: float) -> List[Tuple[int, int]]:
    """Confirmed swing pivots as ``(bar index, +1 high | -1 low)``, alternating."""
    # Tech: track the running extreme in the current direction; a retrace of
    #       `swing` from it confirms the extreme as a pivot and flips direction.
    #       Before the first pivot both extremes are tracked until one side's
    #       move reaches `swing`.
    # Why:  a percent threshold (not points) keeps a 2018 swing at ~10 000 and a
    #       2026 swing at ~47 000 the same size. The last, unconfirmed extreme is
    #       never emitted — its leg has no end yet, so it cannot be scored.
    pivots: List[Tuple[int, int]] = []
    if len(prices) == 0:
        return pivots
    hi = lo = float(prices[0])
    hi_i = lo_i = 0
    direction = 0
    ext, ext_i = hi, 0
    for i in range(1, len(prices)):
        p = float(prices[i])
        if direction == 0:
            if p > hi:
                hi, hi_i = p, i
            if p < lo:
                lo, lo_i = p, i
            if hi >= lo * (1 + swing) and hi_i > lo_i:
                pivots.append((lo_i, -1))
                direction, ext, ext_i = 1, hi, hi_i
            elif lo <= hi * (1 - swing) and lo_i > hi_i:
                pivots.append((hi_i, 1))
                direction, ext, ext_i = -1, lo, lo_i
        elif direction == 1:
            if p > ext:
                ext, ext_i = p, i
            elif p <= ext * (1 - swing):
                pivots.append((ext_i, 1))
                direction, ext, ext_i = -1, p, i
        else:
            if p < ext:
                ext, ext_i = p, i
            elif p >= ext * (1 + swing):
                pivots.append((ext_i, -1))
                direction, ext, ext_i = 1, p, i
    return pivots


def position_series(timestamps: np.ndarray, trades: pd.DataFrame) -> np.ndarray:
    """Signed position held over each bar interval ``(i, i+1]``: +1, -1 or 0."""
    # Tech: a trade's fills land between minute stamps; the first bar at or
    #       after the entry fill opens the exposure, the first bar at or after
    #       the exit fill ends it.
    # Why:  `trades.csv` is the only artifact every run writes with both fill
    #       instants, and `max_position` is 1, so a sign is the whole position.
    pos = np.zeros(len(timestamps), dtype=np.int8)
    if trades.empty:
        return pos
    entry = np.searchsorted(timestamps, trades["entry_ts"].values.astype("datetime64[ns]"))
    exit_ = np.searchsorted(timestamps, trades["exit_ts"].values.astype("datetime64[ns]"))
    sign = np.where(trades["side"].values == "LONG", 1, -1)
    for a, e, s in zip(entry, exit_, sign):
        pos[a:e] = s
    return pos


def score_legs(prices: np.ndarray, pos: np.ndarray,
               pivots: List[Tuple[int, int]]) -> pd.DataFrame:
    """One row per confirmed leg: ride, exposure at the turn, reaction bars."""
    # Tech: a leg runs from pivot k to pivot k+1 with direction -kind_k (down
    #       after a high). Per bar, the loss while against the leg is
    #       -pos * dp on bars where pos * direction < 0; the leg's ride is that
    #       sum over the leg's size. Reaction is the bars from the pivot until
    #       the position first stops opposing the leg, censored at the leg end.
    # Why:  summing bar P&L (rather than pivot-to-exit) also charges a
    #       counter-trend entry made mid-leg, the other half of reacting late.
    #       Bar closes, not fill prices, so the ride is defined for every bar.
    dp = np.diff(prices)
    rows = []
    for (i, kind), (j, _) in zip(pivots[:-1], pivots[1:]):
        direction = -kind
        leg = abs(prices[j] - prices[i])
        seg = pos[i:j].astype(float)
        against = seg * direction < 0
        lost = float(-(seg[against] * dp[i:j][against]).sum())
        exposed = bool(against[0]) if len(against) else False
        react = None
        censored = False
        if exposed:
            freed = np.flatnonzero(~against)
            if len(freed):
                react = int(freed[0])
            else:
                react, censored = j - i, True
        rows.append(dict(pivot=i, leg_end=j, kind=kind, leg_pts=leg,
                         ride=lost / leg if leg else 0.0, exposed=exposed,
                         react_bars=react, censored=censored))
    return pd.DataFrame(rows)


def window_bounds(params: dict, forecasts_head: pd.Timestamp,
                  window: str) -> Tuple[pd.Timestamp, pd.Timestamp]:
    # Tech: train = first forecast -> train_split_ts; test = split -> end;
    #       all = first forecast -> end.
    # Why:  legs inside warmup carry no position by construction and would
    #       dilute the mean toward 0, and a train score must not read a leg that
    #       ends in the held-out tail (plan §6: the tail is scored once).
    split = params.get("train_split_ts")
    end = pd.Timestamp.max
    if window == "all":
        return forecasts_head, end
    if split is None:
        raise SystemExit(f"window={window!r} needs a run with a train split "
                         f"(params.json has no train_split_ts)")
    split = pd.Timestamp(split)
    return (forecasts_head, split) if window == "train" else (split, end)


def load_run(run: Path) -> Tuple[dict, pd.DataFrame, pd.Timestamp]:
    params = json.loads((run / "params.json").read_text(encoding="utf-8"))
    trades = pd.read_csv(run / "trades.csv", parse_dates=["entry_ts", "exit_ts"])
    head = pd.read_csv(run / "forecasts.csv", nrows=1, parse_dates=["timestamp"])
    return params, trades, pd.Timestamp(head["timestamp"].iloc[0])


def evaluate(run: Path, tape: pd.DataFrame, *, swing: float = SWING,
             window: str = "train",
             pivots: Optional[List[Tuple[int, int]]] = None) -> Dict:
    """Score one run folder against ``tape``; returns summary + per-leg frame."""
    params, trades, head = load_run(run)
    ts = tape["timestamp"].values.astype("datetime64[ns]")
    px = tape["price"].values.astype(float)
    if pivots is None:
        pivots = zigzag(px, swing)
    legs = score_legs(px, position_series(ts, trades), pivots)
    lo, hi = window_bounds(params, head, window)
    if not legs.empty:
        start = pd.to_datetime(ts[legs["pivot"].values])
        end = pd.to_datetime(ts[legs["leg_end"].values])
        legs = legs[(start >= lo) & (end < hi)].reset_index(drop=True)
    exp = legs[legs["exposed"]] if not legs.empty else legs
    summary = {
        "run": str(run), "window": window, "swing": swing, "legs": int(len(legs)),
        "reversal_ride": float(legs["ride"].mean()) if len(legs) else float("nan"),
        "exposed_at_turn": int(len(exp)),
        "ride_at_turn_median": float(exp["ride"].median()) if len(exp) else float("nan"),
        "react_hours_median": float(exp["react_bars"].median() / 60) if len(exp) else float("nan"),
        "censored_at_turn": float(exp["censored"].mean()) if len(exp) else float("nan"),
        "train_split_ts": params.get("train_split_ts"),
        "warmup_bars": params.get("warmup_bars"),
    }
    return {"summary": summary, "legs": legs, "params": params}


def paired_delta(run_legs: pd.DataFrame, base_legs: pd.DataFrame,
                 *, n_boot: int = BOOTSTRAP, seed: int = SEED) -> Dict:
    """Mean per-leg ride difference (run - baseline) with a bootstrap 95 % CI."""
    # Tech: join the two runs on the pivot bar, difference the ride per leg,
    #       resample legs with replacement for the CI of the mean difference.
    # Why:  both runs face the same reversals, so the pairing removes the
    #       leg-to-leg spread (one ride is 0.0, the next 1.0) that would
    #       otherwise swamp a real improvement — the unpaired SE at ~240 legs is
    #       several times the paired one. Fixed seed: a re-run prints the same CI.
    m = run_legs[["pivot", "ride"]].merge(base_legs[["pivot", "ride"]],
                                          on="pivot", suffixes=("", "_base"))
    if m.empty:
        return {"paired_legs": 0, "delta_ride": float("nan"),
                "ci_low": float("nan"), "ci_high": float("nan")}
    d = (m["ride"] - m["ride_base"]).to_numpy()
    rng = np.random.default_rng(seed)
    boots = d[rng.integers(0, len(d), size=(n_boot, len(d)))].mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {"paired_legs": int(len(d)), "delta_ride": float(d.mean()),
            "ci_low": float(lo), "ci_high": float(hi),
            "legs_better": int((d < 0).sum()), "legs_worse": int((d > 0).sum())}


def _comparable(a: dict, b: dict) -> List[str]:
    # Why: a paired delta across different tapes or splits pairs different
    #      reversals by bar index — numbers that look fine and mean nothing.
    return [k for k in ("data", "warmup_bars", "train_split_bars")
            if a.get(k) != b.get(k)]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run", type=Path, help="a real_data run folder")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="run folder to pair against (the incumbent)")
    ap.add_argument("--window", choices=("train", "test", "all"), default="train")
    ap.add_argument("--swing", type=float, default=SWING)
    ap.add_argument("--data", type=Path, default=None,
                    help="tape override (default: the run's params.json data)")
    args = ap.parse_args(argv)

    from ..real_data import load_minute_bars
    params = json.loads((args.run / "params.json").read_text(encoding="utf-8"))
    tape = load_minute_bars(args.data or Path(params["data"]), None)
    px = tape["price"].to_numpy(dtype=float)
    pivots = zigzag(px, args.swing)

    res = evaluate(args.run, tape, swing=args.swing, window=args.window, pivots=pivots)
    s = res["summary"]
    print(f"[reversal] window={s['window']} swing={s['swing']:.3f} legs={s['legs']}")
    print(f"  reversal_ride          {s['reversal_ride']:.4f}   (mean share of a swing ridden against; lower = quicker)")
    print(f"  exposed_at_turn        {s['exposed_at_turn']}")
    print(f"  ride_at_turn_median    {s['ride_at_turn_median']:.4f}")
    print(f"  react_hours_median     {s['react_hours_median']:.1f}   (session hours, pivot -> off the wrong side)")
    print(f"  censored_at_turn       {s['censored_at_turn']:.2f}   (rode the whole leg)")
    out = dict(s)

    if args.baseline is not None:
        base = evaluate(args.baseline, tape, swing=args.swing, window=args.window, pivots=pivots)
        bad = _comparable(res["params"], base["params"])
        if bad:
            print(f"refusing to pair: runs differ on {bad}", file=sys.stderr)
            return 2
        pd_ = paired_delta(res["legs"], base["legs"])
        b = base["summary"]
        print(f"[reversal vs baseline] {args.baseline}")
        print(f"  baseline_ride          {b['reversal_ride']:.4f}")
        print(f"  delta_ride             {pd_['delta_ride']:+.4f}   CI95 [{pd_['ci_low']:+.4f}, {pd_['ci_high']:+.4f}]")
        print(f"  legs better/worse      {pd_.get('legs_better', 0)} / {pd_.get('legs_worse', 0)} of {pd_['paired_legs']}")
        out["baseline"] = str(args.baseline)
        out["baseline_ride"] = b["reversal_ride"]
        out.update(pd_)

    (args.run / f"reversal_{args.window}.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
