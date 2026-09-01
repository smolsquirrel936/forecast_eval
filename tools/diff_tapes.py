"""TAPE_REBUILD_PLAN T4 — the old tape against the new one, attributed by cause.

A bar that moved between the shipped tape and the rebuilt one has **four**
possible reasons, and the plan is explicit that a table without the attribution
cannot be read (§3.1, §0.6): §0.6 measures ~14 % of recent closes moving for
reasons that have nothing to do with the splice, so a bare "18 % of closes
changed" would be read as the front-month rule and would be wrong.

The separation is exact rather than estimated, and it rests on one fact T1
established: applying the **old** `min(contract)` rule to the per-contract
intermediate reproduces the shipped tape's row index *exactly*. That gives a
middle term, and the two halves either side of it isolate the causes cleanly:

```
OLD   the shipped tape          _prerebuild_backup_20260901/
 |      <- C (unstable sort: open/close) and D (duplicated files: volume)
MID   old front-month rule, rebuilt from the intermediate   (built here)
 |      <- A (the splice instant) and B (out-of-session rows dropped)
NEW   the rebuilt tape          dataset/RPT/
```

OLD→MID holds the row set fixed, so every difference is a *value* difference and
must be C or D. MID→NEW shares one source and one ordering, so no value can move
unless the minute changed contract — which the run asserts: **0 fields differ on
any minute whose contract did not change**. Without that assertion the two halves
would not be separable and every number below would be an estimate.
"""
from __future__ import annotations

import argparse
import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

from forecast_eval.environment import classify_session

TAPES = ("TXF_RPT_minute_day.csv", "TXF_RPT_minute_night.csv", "TXF_RPT_minute.csv")
FIELDS = ["open", "high", "low", "close", "volume"]

# Tech: the session split the shipped tapes were made with — DAY is 08:45
#       through 13:45 *inclusive*, everything else is NIGHT.
# Why:  measured on the pre-rebuild files, not taken from the notebook: the old
#       day tape's time-of-day runs 08:45–13:45 and the old night tape holds
#       00:00–05:00 and 15:00–23:59. That inclusive 13:45 is the whole reason the
#       old tapes carry 1 297 bars `classify_session` calls out-of-session, and
#       reproducing it is what makes MID's row index match OLD's exactly.
OLD_DAY_OPEN, OLD_DAY_CLOSE = dtime(8, 45), dtime(13, 45)


def old_rule_tape(bars: pd.DataFrame) -> pd.DataFrame:
    """The shipped pipeline's front-month rule, applied to T1's intermediate."""
    # Tech: per calendar date of the print, keep the numerically smallest month.
    # Why:  `process_txf.ipynb` cell 7, restated — `groupby("trading_date")
    #       ["contract_int"].min()`. `trading_date` is the calendar date of the
    #       print rather than the session's trading day (verified in T1), so
    #       `datetime.dt.normalize()` is the same key. This is the rule that rolls
    #       at the 00:00 boundary and splices one day after expiry (§0.1).
    front = bars.groupby(bars["datetime"].dt.normalize())["contract"].transform("min")
    return (bars[bars["contract"].to_numpy() == front.to_numpy()]
            .sort_values("datetime", kind="stable").reset_index(drop=True))


def old_session_split(mid: pd.DataFrame, name: str) -> pd.DataFrame:
    is_day = ((mid["datetime"].dt.time >= OLD_DAY_OPEN)
              & (mid["datetime"].dt.time <= OLD_DAY_CLOSE))
    if name.endswith("_day.csv"):
        return mid[is_day].reset_index(drop=True)
    if name.endswith("_night.csv"):
        return mid[~is_day].reset_index(drop=True)
    return mid


def _spread(values: np.ndarray) -> str:
    if not len(values):
        return "—"
    return (f"median {np.median(values):.0f} / p95 "
            f"{np.percentile(values, 95):.0f} / max {values.max():.0f}")


def report(old: pd.DataFrame, mid: pd.DataFrame, new: pd.DataFrame,
           name: str) -> bool:
    """Print one tape's attributed diff; return False if an invariant broke."""
    print(f"\n{'=' * 78}\n{name}   OLD {len(old):,} -> NEW {len(new):,} "
          f"({len(new) - len(old):+,})\n{'=' * 78}")

    # Tech: MID must reproduce OLD's row index and contract column exactly.
    # Why:  the load-bearing assumption. If the old rule replayed on the new
    #       intermediate did *not* land on the same minutes, then OLD→MID would
    #       mix row changes into what is supposed to be a pure value diff, and the
    #       attribution below would be arithmetic on incomparable things.
    exact = (len(mid) == len(old) and mid["datetime"].equals(old["datetime"])
             and (mid["contract"].to_numpy() == old["contract"].to_numpy()).all())
    print(f"  MID reproduces OLD's index and contracts exactly: {exact}")
    if not exact:
        print("  ABORT: the attribution below is not valid without this.")
        return False

    # ---- C and D: OLD vs MID, same rows -----------------------------------
    # Tech: field-by-field inequality between the shipped values and the faithful
    #       rebuild of the same rows.
    # Why:  §0.6's two defects and nothing else can live here. They separate by
    #       *which* field moves: `open`/`close` are the order-dependent
    #       aggregations (Defect C, the unstable `sort_index()`), `volume` is the
    #       summed one (Defect D, whole-file duplication). `high`/`low` are
    #       order- and duplicate-independent, so a non-zero there would mean the
    #       rebuild changed the print set and both attributions are void.
    diff = {f: old[f].to_numpy() != mid[f].to_numpy() for f in FIELDS}
    d_close = np.abs(old["close"].to_numpy() - mid["close"].to_numpy())[diff["close"]]
    print(f"\n  [C] unstable sort   open {diff['open'].sum():>8,}"
          f"   close {diff['close'].sum():>8,}"
          f"   ({100 * diff['close'].sum() / len(old):.2f}% of closes)")
    print(f"      |dclose| {_spread(d_close)}")
    print(f"      high {diff['high'].sum()} / low {diff['low'].sum()}"
          f"  <- must be 0/0: they are order-independent")
    ratio = (old["volume"].to_numpy()[diff["volume"]]
             / mid["volume"].to_numpy()[diff["volume"]])
    print(f"  [D] duplicated files  volume {diff['volume'].sum():>6,}"
          + (f"   old/new ratio {np.median(ratio):.3f} median, "
             f"{ratio.min():.3f}-{ratio.max():.3f}" if len(ratio) else ""))

    # ---- A and B: MID vs NEW ----------------------------------------------
    merged = mid.merge(new, on="datetime", how="outer",
                       suffixes=("_mid", "_new"), indicator=True)
    removed = merged[merged["_merge"] == "left_only"]
    added = merged[merged["_merge"] == "right_only"]
    both = merged[merged["_merge"] == "both"]
    out_of_session = int(removed["datetime"].map(classify_session).isna().sum())
    reattributed = both["contract_mid"].to_numpy() != both["contract_new"].to_numpy()

    # Tech: no field may differ on a minute whose contract stayed the same.
    # Why:  MID and NEW come from one source with one ordering, so the only thing
    #       that can move a value between them is the minute being taken from a
    #       different month. A non-zero here means C or D leaked across the seam
    #       and the split into halves is false.
    leak = sum(int((both[f"{f}_mid"].to_numpy()[~reattributed]
                    != both[f"{f}_new"].to_numpy()[~reattributed]).sum())
               for f in FIELDS)
    d_reatt = np.abs(both["close_mid"].to_numpy()
                     - both["close_new"].to_numpy())[reattributed]
    print(f"\n  [A] front-month rule  added {len(added):,}"
          f"   removed {len(removed) - out_of_session:,}"
          f"   re-attributed {int(reattributed.sum()):,}"
          f" ({100 * reattributed.sum() / len(new):.2f}% of the new tape)")
    print(f"      |dclose| on re-attributed bars: {_spread(d_reatt)}")
    print(f"  [B] out-of-session rows dropped: {out_of_session:,}")
    print(f"\n  fields differing where the contract did NOT change: {leak}"
          f"  <- must be 0")
    if leak:
        print("  ABORT: C/D and A/B are not separable; the split above is false.")
        return False

    # ---- the net, decomposed ----------------------------------------------
    # Tech: on the minutes both tapes carry, split the closes that moved into
    #       "the splice moved it", "the sort moved it", and "both".
    # Why:  this is the table §3.1 asks for and the one T5 needs. The headline
    #       percentage is not the rebuild's doing — on the day tape most of it is
    #       Defect C, which would have moved those closes on any faithful rebuild
    #       whatever the splice rule was.
    shared = old.merge(new, on="datetime", how="inner", suffixes=("_o", "_n"))
    om = old.merge(mid, on="datetime", how="inner", suffixes=("_o", "_m"))
    c_moved = (pd.Series(om["close_o"].to_numpy() != om["close_m"].to_numpy(),
                         index=om["datetime"])
               .reindex(shared["datetime"]).to_numpy())
    a_moved = shared["contract_o"].to_numpy() != shared["contract_n"].to_numpy()
    net = shared["close_o"].to_numpy() != shared["close_n"].to_numpy()
    d_net = np.abs(shared["close_o"].to_numpy() - shared["close_n"].to_numpy())
    print(f"\n  net on the {len(shared):,} minutes both tapes carry: "
          f"close moved on {int(net.sum()):,} "
          f"({100 * net.sum() / len(shared):.2f}%)")
    for label, mask in (("A only  (re-attributed)", net & a_moved & ~c_moved),
                        ("A and C", net & a_moved & c_moved),
                        ("C only  (same contract)", net & ~a_moved & c_moved)):
        print(f"      {label:<26} {int(mask.sum()):>8,}"
              f"   {100 * mask.sum() / max(net.sum(), 1):5.1f}% of what moved")
    print(f"      re-attributed but close unchanged anyway: "
          f"{int((~net & a_moved).sum()):,}")
    print(f"      |dclose| where it moved: {_spread(d_net[net])}")
    return True


def per_roll(mid: pd.DataFrame, new: pd.DataFrame, tail: int) -> None:
    # Tech: added / removed / re-attributed bars grouped by delivery month.
    # Why:  §5's "by roll". The point is the *uniformity*: if the front-month
    #       change were concentrated in a few months it would be a data accident,
    #       and if it recurs at every expiry it is the rule. ~540 added a month is
    #       §0.2's "~540 missing per roll" arriving from the other direction.
    merged = mid.merge(new, on="datetime", how="outer",
                       suffixes=("_mid", "_new"), indicator=True)
    month = merged["contract_new"].fillna(merged["contract_mid"])
    frame = pd.DataFrame({
        "expiry": month,
        "added": merged["_merge"].eq("right_only"),
        "removed": merged["_merge"].eq("left_only"),
        "reattributed": (merged["_merge"].eq("both")
                         & (merged["contract_mid"].to_numpy()
                            != merged["contract_new"].to_numpy())),
    })
    table = frame.groupby("expiry")[["added", "removed", "reattributed"]].sum()
    print(f"\n  per delivery month (last {tail} of {len(table)}):")
    print(table.tail(tail).to_string().replace("\n", "\n    "))
    touched = int((table.sum(axis=1) > 0).sum())
    print(f"    totals: added {int(table['added'].sum()):,}, "
          f"removed {int(table['removed'].sum()):,}, "
          f"re-attributed {int(table['reattributed'].sum()):,}; "
          f"{touched} of {len(table)} months touched")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bycontract", type=Path, required=True,
                    help="TXF_RPT_minute_bycontract.csv from T1")
    ap.add_argument("--old-dir", type=Path, required=True,
                    help="the pre-rebuild tapes, e.g. _prerebuild_backup_20260901/")
    ap.add_argument("--new-dir", type=Path, required=True)
    ap.add_argument("--tapes", nargs="*", default=list(TAPES))
    ap.add_argument("--per-roll", default="TXF_RPT_minute_night.csv",
                    help="tape to break down by delivery month ('' for none)")
    ap.add_argument("--roll-tail", type=int, default=14)
    args = ap.parse_args(argv)

    read = lambda p: pd.read_csv(p, parse_dates=["datetime"],
                                 dtype={"contract": str})
    print(f"reading {args.bycontract}", flush=True)
    mid_all = old_rule_tape(read(args.bycontract))
    print(f"  old rule replayed: {len(mid_all):,} bars", flush=True)

    ok = True
    for name in args.tapes:
        mid = old_session_split(mid_all, name)
        old, new = read(args.old_dir / name), read(args.new_dir / name)
        ok &= report(old, mid, new, name)
        if name == args.per_roll and ok:
            per_roll(mid, new, args.roll_tail)
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
