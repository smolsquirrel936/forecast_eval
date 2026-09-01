"""TAPE_REBUILD_PLAN T1 — minute OHLCV keyed by (contract, minute), all months.

**Why this exists instead of rebuilding `TXF_RPT_ticks.csv`.** §3's pipeline runs
one front-month rule at notebook cell 7, writes a front-filtered tick file, and
derives the minute tapes by splitting it. That structure carries exactly **one**
splice instant, and §2.3 (as amended) needs **two**: day and the combined
full-day tape splice at 12:45, night at 15:30. A front-filtered tick file cannot
express both, so the front-month selection has to happen *after* this stage, not
before it.

So this pass does the expensive part **once** — read 3 832 raw `.rpt` files,
~55 GB — and stops short of choosing a front month. It keeps the **two nearest
listed months** per calendar date (measured on a roll date: they are 99.88 % of
TX volume; the rest are far months nothing trades) and aggregates to minute bars
per contract. Every tape is then `front_month_series(ts, policy)` applied to this
file: cheap, repeatable, and a fourth deployment later is a filter rather than a
55 GB rebuild.

**The timestamp convention, verified 2026-09-01 rather than assumed.** `time` is
`HHMMSSCC` (older files `HHMMSS`), and `trading_date` is the **calendar date of
the print**, not the session's trading day — measured across three consecutive
files: `Daily_2026_08_18.rpt` carries Monday's night head as
`20260817 15:00–23:00` and its tail as `20260818 00:00–05:00`. So concatenating
`trading_date + time` yields the correct wall-clock instant with no midnight
adjustment, which is what the notebook already did at cell 11.

That same fact is the defect's mechanism: grouping by `trading_date` and taking
`min(contract)` rolls the front month at the **00:00 calendar boundary**, which
splits one night session across two labels. `dataset/NOTE.md` diagnosed exactly
this ("rolls the front-month contract at the calendar-day boundary (00:00)
instead of at the night-session boundary").
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from pathlib import Path

import pandas as pd

COL_NAMES = ["trading_date", "product_code", "contract",
             "time", "price", "quantity", "near_price", "far_price"]
OUT_COLS = ["datetime", "contract", "open", "high", "low", "close", "volume"]
N_NEAREST = 2


def read_rpt(path: Path) -> pd.DataFrame:
    """One raw file -> TX prints with a wall-clock `datetime`, spreads dropped."""
    # Tech: `usecols=range(8)` because files after 2015 carry a 9th column.
    # Why:  the notebook's own reason, kept so this and cell 5 cannot drift.
    df = pd.read_csv(path, encoding="cp950", header=None, names=COL_NAMES,
                     usecols=list(range(8)), dtype=str, skiprows=1,
                     on_bad_lines="skip")
    df = df.apply(lambda c: c.str.strip() if c.dtype == "object" else c)
    tx = df[df["product_code"] == "TX"]
    if tx.empty:
        return pd.DataFrame(columns=OUT_COLS)

    # Tech: coerce the contract to an int, dropping what will not parse.
    # Why:  calendar spreads are coded `201101/201102` and are not a delivery
    #       month; `to_numeric(errors="coerce")` + dropna is how cell 7 removed
    #       them and is kept identical here.
    contract_int = pd.to_numeric(tx["contract"], errors="coerce")
    tx = tx.loc[contract_int.notna()].copy()
    tx["contract_int"] = contract_int.loc[contract_int.notna()].astype(int)

    ts = pd.to_datetime(tx["trading_date"] + tx["time"].str[:6].str.zfill(6),
                        format="%Y%m%d%H%M%S", errors="coerce")
    tx = tx.loc[ts.notna()].copy()
    tx["datetime"] = ts.loc[ts.notna()]
    tx["price"] = pd.to_numeric(tx["price"], errors="coerce")
    tx["quantity"] = pd.to_numeric(tx["quantity"], errors="coerce")
    tx = tx.dropna(subset=["price"])

    # Tech: keep the N nearest listed months per calendar date.
    # Why:  the front-month choice belongs to the splice rule, not here, and any
    #       rule between two adjacent months needs both present. Two is enough:
    #       on 2026-08-18 the two nearest were 86 903 of 87 007 TX prints.
    keep = (tx.groupby(tx["datetime"].dt.date)["contract_int"]
              .transform(lambda s: s.rank(method="dense") <= N_NEAREST))
    return tx.loc[keep]


def to_minute_bars(tx: pd.DataFrame) -> pd.DataFrame:
    """Minute OHLCV per (minute, contract), left-labelled."""
    if tx.empty:
        return pd.DataFrame(columns=OUT_COLS)
    # Tech: stable-sort by timestamp before grouping, exactly as notebook cell 11
    #       does with `set_index("datetime").sort_index()`.
    # Why:  MEASURED — without it `high`/`low`/`volume` still reproduce the
    #       existing tape bit-for-bit but `open`/`close` drift by up to 6 points.
    #       Those two are the only order-dependent aggregations, and `time` is
    #       truncated to seconds while the raw carries centiseconds, so several
    #       prints share a key and their order decides first/last. This rebuild
    #       must change the front-month rule and nothing else, so the tie-break
    #       has to be the one the pinned tape was built with.
    tx = tx.sort_values("datetime", kind="stable")
    g = tx.groupby([tx["datetime"].dt.floor("min"), "contract"])
    bars = g["price"].agg(open="first", high="max", low="min", close="last")
    bars["volume"] = g["quantity"].sum()
    return bars.dropna(subset=["open"]).reset_index().rename(
        columns={"datetime": "datetime"})[OUT_COLS]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, required=True,
                    help="extracted_data/ directory of Daily_*.rpt files")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N files (smoke test)")
    args = ap.parse_args(argv)

    files = sorted(f for f in args.src.iterdir()
                   if f.suffix in (".rpt", ".csv") and f.name.startswith("Daily_"))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"ERROR: no Daily_*.rpt under {args.src}", file=sys.stderr)
        return 2

    print(f"{len(files)} files -> {args.out}", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    t0 = _time.time()
    written = 0
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        for i, path in enumerate(files, 1):
            try:
                bars = to_minute_bars(read_rpt(path))
            except Exception as exc:                       # noqa: BLE001
                print(f"  [warn] {path.name}: {type(exc).__name__}: {exc}",
                      flush=True)
                continue
            if len(bars):
                bars.to_csv(fh, index=False, header=(written == 0),
                            lineterminator="\n")
                written += len(bars)
            if i % 250 == 0 or i == len(files):
                el = _time.time() - t0
                print(f"  {i}/{len(files)}  {written:,} bars  "
                      f"{el:.0f}s  eta {el / i * (len(files) - i):.0f}s",
                      flush=True)

    print(f"  raw pass: {written:,} bars in {_time.time() - t0:.0f}s", flush=True)

    # Tech: one global pass to drop duplicate (datetime, contract) bars, then
    #       sort. Per-file dedup cannot do it -- the duplicates span files.
    # Why:  MEASURED 2026-09-01, and not in TAPE_REBUILD_PLAN. A few raw files
    #       carry a STALE copy of an earlier session instead of their own:
    #       `Daily_2026_07_13.rpt` repeats 2026-07-09's night block (29 899
    #       prints, byte-identical) and 2026-07-10's night session is absent
    #       from every file as a result. The notebook concatenated both copies
    #       and summed `quantity`, so the shipped tape carries DOUBLED volume
    #       across those minutes -- 2026-07-09 15:00 reads 1 596 lots where the
    #       source has 798. `high`/`low` are untouched (duplicating identical
    #       prints cannot move a min or a max), which is why this hid: only
    #       `volume` betrays it. Rare -- one session in 153 across 2026 -- but
    #       silent, so it is dropped here rather than documented.
    print("  dedupe + sort...", flush=True)
    df = pd.read_csv(args.out)
    before = len(df)
    dup = df.duplicated(subset=["datetime", "contract"], keep=False)
    n_dup_rows = int(dup.sum())
    if n_dup_rows:
        # Tech: check the duplicates really are identical before collapsing them.
        # Why:  dropping a *conflicting* row would be a silent data choice. If
        #       two files ever disagree about the same minute this must be seen,
        #       not resolved by keep="first".
        g = df[dup].groupby(["datetime", "contract"])[
            ["open", "high", "low", "close", "volume"]].nunique().max().max()
        if g > 1:
            print(f"  [WARN] duplicate bars DISAGREE (max {g} distinct values "
                  f"per field) -- inspect before trusting this tape", flush=True)
        else:
            print(f"  duplicates are byte-identical ({n_dup_rows:,} rows)",
                  flush=True)
    df = (df.drop_duplicates(subset=["datetime", "contract"], keep="first")
            .sort_values(["datetime", "contract"], kind="stable")
            .reset_index(drop=True))
    df.to_csv(args.out, index=False, lineterminator="\n")

    print(f"done: {before:,} -> {len(df):,} bars "
          f"({before - len(df):,} duplicates dropped) in "
          f"{_time.time() - t0:.0f}s -> {args.out}")
    print(f"  span      : {df.datetime.min()} .. {df.datetime.max()}")
    print(f"  contracts : {df.contract.nunique()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
