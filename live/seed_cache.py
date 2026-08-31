"""Seed a live bar cache offline from an RPT minute file (DAY_SESSION_PLAN C7).

A first DAY run cannot fetch the context it needs. C6 sizes the emitter prime at
~85 calendar days for a DAY session; C7 is why that window is unreachable from
the venue: expired months are delisted (SHIOAJI_PLAN §2.8 Finding D) and
`cli.py::_earlier_months` fetches only the front month plus one, so an 85-day
window spans ~3 delivery months of which the older two no longer answer `kbars`.
The bar cache is the only other source and it **starts empty**, so the run is
short ~2 months of context and cannot recover except by waiting.

This module fills it from the RPT tape the backtest already reads, in the
cache's own four-column schema, so a first run starts warm.

**It mixes two bar sources in one context window** — RPT here, `kbars` at run
time — which V1c only ever compared on a single session. DAY_SESSION_PLAN D4
accepted that trade on 2026-08-24: the alternative was waiting ~2 months for a
cache to fill from live. What makes it defensible is that the two are the same
observations under the same labels: `fetch_bars_continuous` converts `kbars`'
right-labelled bars to this file's left-labelled convention before anything else
(`backfill.py`'s `source_label` shift), and both drop NaN closes and carry real
per-bar volume.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import pandas as pd

from forecast_eval.live.backfill import BAR_COLUMNS, CACHE_KEEP_BARS, save_bar_cache
from forecast_eval.live.sessions import SessionPolicy


def rpt_to_cache_frame(path: "str | Path", policy: SessionPolicy, *,
                       keep: int = CACHE_KEEP_BARS,
                       until: Optional[str] = None) -> pd.DataFrame:
    """The trailing `keep` bars of `path` that `policy` trades, cache-shaped."""
    # Tech: read the RPT file, normalise its column names, and collapse any
    #       minute carrying more than one contract to the highest-volume row.
    # Why:  the collapse mirrors `real_data.load_minute_bars` exactly. It has
    #       never had to fire (measured 2026-08-23: zero such minutes across all
    #       three RPT files), but the seeded cache must be the same series the
    #       backtest evaluated, so the rule is reproduced rather than assumed
    #       unnecessary.
    df = pd.read_csv(path, dtype={"contract": str})
    if "datetime" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"datetime": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if "contract" in df.columns:
        df = df.loc[df.groupby("timestamp")["volume"].idxmax()].reset_index(drop=True)

    # Tech: drop NaN closes and sort, as the offline loader does.
    # Why:  Toto2 propagates NaN, so a gap left in the cache poisons the context
    #       it was seeded to provide.
    df = df.dropna(subset=["close"]).sort_values("timestamp").reset_index(drop=True)

    # Tech: RPT labels a bar by the minute it opens; no shift is applied here.
    # Why:  this file *is* the convention `backfill.py` converts `kbars` into, so
    #       shifting would double-apply a correction that only the venue's
    #       right-labelled feed needs.
    if until is not None:
        df = df[df["timestamp"] <= pd.Timestamp(until)]

    # Tech: keep only the bars this deployment's policy trades.
    # Why:  `runtime.warm_up` rejects a warm-up frame containing bars from any
    #       other session, so an unfiltered seed would not merely be untidy — it
    #       would make every start-up raise. Filtering here is also what makes
    #       the seed honest: a DAY cache holds DAY bars and nothing else.
    keepmask = df["timestamp"].map(policy.includes)
    df = df[keepmask]

    out = pd.DataFrame({
        "timestamp": df["timestamp"],
        "price": df["close"].astype(float),
        "volume": df["volume"].astype(float) if "volume" in df.columns else 1.0,
        "contract": df["contract"] if "contract" in df.columns else "",
    })
    return out.tail(keep).reset_index(drop=True)[BAR_COLUMNS]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True,
                    help="RPT minute csv to seed from, e.g. TXF_RPT_minute_day.csv")
    ap.add_argument("--out", type=Path, required=True,
                    help="cache path to write, e.g. outputs/bars_cache_day.csv")
    ap.add_argument("--session", choices=("day", "night"), required=True,
                    help="which deployment this cache is for")
    ap.add_argument("--keep", type=int, default=CACHE_KEEP_BARS)
    ap.add_argument("--until", default=None,
                    help="drop bars after this timestamp (for reproducible seeds)")
    args = ap.parse_args(argv)

    policy = SessionPolicy.day() if args.session == "day" else SessionPolicy.night()
    bars = rpt_to_cache_frame(args.data, policy, keep=args.keep, until=args.until)
    if bars.empty:
        print(f"ERROR: no {args.session.upper()} bars found in {args.data}")
        return 2
    save_bar_cache(args.out, bars, keep=args.keep)
    print(f"seeded {len(bars)} {args.session.upper()} bars -> {args.out}")
    print(f"  range: {bars['timestamp'].iloc[0]} .. {bars['timestamp'].iloc[-1]}")
    print(f"  months: {sorted(bars['contract'].unique())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
