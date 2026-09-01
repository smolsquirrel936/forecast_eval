"""TAPE_REBUILD_PLAN T2/T3 — the three evaluated tapes, from T1's intermediate.

T1 stopped one step short of choosing a front month, on purpose: the three
deployments splice at **two different instants** (day and the combined full-day
tape at 12:45, night at 15:30) and none of the three tapes is a session-split of
another, so no single front-filtered series can produce them (§2.3). This module
is the step that was deferred. Each tape is one filter over
`TXF_RPT_minute_bycontract.csv`:

```
TXF_RPT_minute_bycontract.csv        (4 551 359 bars, two months per minute)
  +- front_month_series(ts, day())   -> TXF_RPT_minute_day.csv     12:45, DAY rows
  +- front_month_series(ts, both())  -> TXF_RPT_minute.csv         12:45, DAY+NIGHT
  +- front_month_series(ts, night()) -> TXF_RPT_minute_night.csv   15:30, NIGHT rows
```

**T3 is folded in.** §3's diagram had the session split as a separate pass over
one front-filtered file; here the split *is* the front-month selection, because
which month a minute belongs to now depends on which deployment is asking.

**The selection is `live/backfill.collapse_front_month`'s rule, not a second
copy of it.** Front month if it printed that minute, otherwise the most-traded
other month — the fallback exists because "a minute the front month did not print
is better filled from the other month than dropped", and the offline tape not
having it is precisely the structural gap §1 is about. The rule is applied here
rather than imported because `collapse_front_month` works on the live path's
`(timestamp, price, volume, contract)` bar frame while the tape is OHLCV keyed on
`datetime`; the ordering — front-ness first, volume as tie-break, keep last — is
identical and `tests/test_tape_build.py` pins that.
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd

from forecast_eval.environment import classify_session
from forecast_eval.live.roll import front_month_series, last_trading_day
from forecast_eval.live.sessions import SessionPolicy

# Tech: the first minute the full-day tape carries.
# Why:  MEASURED 2026-09-01. The combined tape spans 2011-01-03, but TAIFEX's
#       after-hours session did not exist until **2017-05-15**, so its first
#       468 524 bars (15.6 %) are DAY-only — a 300-bar "session" where a full-day
#       deployment's is 1 140. Night hours were 15:00–04:59 from day one (checked:
#       the post-midnight tail ends 04:59 in every month), so the ramp is
#       *liquidity*, not a schedule change: median night coverage runs 0.84–0.93
#       through 2017 and first reaches the ~0.99 plateau in **2018-02**.
#
#       2018-01-01 is the cut: a round boundary that excludes the whole 2017
#       ramp. January 2018 sits at 0.931, still short of the plateau — kept
#       because one transitional month out of 8.6 years is a smaller distortion
#       than an unexplainable start date, and stated here so it is not a
#       surprise. Move this to 2018-02-01 for the strict reading, or to
#       2017-05-15 to take every full-day minute that exists.
FULLDAY_START = pd.Timestamp("2018-01-01")

# Tech: the four outputs, as (filename, policy factory, first minute or None).
# Why:  the filename *is* the deployment, and the policy is what supplies both
#       the splice instant (`roll_at_time`) and the session filter. Keeping them
#       adjacent in one table is what stops the pairing being re-derived — the
#       combined file's 12:45 is not guessable from its name (§2.3, D2).
#
#       The last two share a policy and differ only in where they start.
#       `TXF_RPT_minute.csv` stays the complete record — every in-session minute
#       the source has, which the DAY deployment's history genuinely needs back
#       to 2011 — while `TXF_RPT_minute_fullday.csv` is the slice a full-day
#       deployment can actually be tuned on. One file cannot be both: truncating
#       the combined tape would discard six years of real day-session data, and
#       tuning on it whole would span two different session structures.
TAPES = (
    ("TXF_RPT_minute_day.csv", SessionPolicy.day, None),
    ("TXF_RPT_minute_night.csv", SessionPolicy.night, None),
    ("TXF_RPT_minute.csv", SessionPolicy.both, None),
    ("TXF_RPT_minute_fullday.csv", SessionPolicy.both, FULLDAY_START),
)

OUT_COLS = ["datetime", "contract", "open", "high", "low", "close", "volume"]
OHLCV = ["open", "high", "low", "close", "volume"]

# Tech: the time of day the expiring month stops trading on its last trading day.
# Why:  MEASURED (§0.2) — TAIFEX retires it at 13:30 while every other month runs
#       to 13:44:59 and on into the night. It is the cutoff the dead-contract
#       check below tests against, and the reason the old `min(contract)` rule
#       lost ~540 night bars and 15 day bars every month.
SETTLEMENT_CLOSE = dtime(13, 30)


def session_labels(stamps: pd.Series) -> pd.Series:
    """`classify_session` for a whole column, via a time-of-day lookup."""
    # Tech: build the answer for each distinct time of day once, then map.
    # Why:  exactly equivalent, because `classify_session` reads `ts.time()` and
    #       nothing else — there are at most 1 440 distinct answers behind 4.5 M
    #       rows. Calling the real function on the lookup keys rather than
    #       re-deriving the windows is what keeps the tape and `run_backtest` from
    #       ever disagreeing about what a session is.
    tod = stamps.dt.time
    lut = {t: classify_session(datetime.combine(date(2020, 1, 1), t))
           for t in tod.unique()}
    return tod.map(lut)


def front_month_tape(bars: pd.DataFrame, labels: pd.Series,
                     policy: SessionPolicy) -> pd.DataFrame:
    """One deployment's tape: front-month-continuous, its sessions only."""
    # Tech: rank each candidate row by (is it the calendar's front month, volume)
    #       and keep the best one per minute.
    # Why:  `collapse_front_month`'s ordering, for the reason given there — the
    #       calendar rule is the one the live run switches on, and volume is the
    #       *fallback* rather than the rule because at a roll the expiring month
    #       usually still has the larger volume. A stable sort matters: it keeps
    #       the intermediate's own contract order for rows this cannot separate.
    want = front_month_series(bars["datetime"], policy)
    ranked = bars.assign(_front=(bars["contract"].to_numpy() == want))

    # Tech: a minute the front month did not print may be filled only from an
    #       *earlier* delivery month, never a later one.
    # Why:  that is the reach `fetch_bars_continuous` actually has — "the front
    #       month plus whatever earlier months are still listed", plus the cache
    #       that carries a delisted month. It can never fetch the far month, so a
    #       tape that filled from one would be a series the live run cannot
    #       reproduce, which is the exact class of divergence this rebuild exists
    #       to close (§1, §7). MEASURED 2026-09-01: the restriction confines the
    #       fallback to the ~47 minutes per post-roll window where the incoming
    #       month is thin — 10 240 night bars over 216 windows — and rejects
    #       2 048 bars sourced from a month live cannot see.
    #
    #       It is worth knowing what the fallback costs even so. Those bars carry
    #       the inter-month basis (§0.4: 84–325 points), so `|Δclose|` on them is
    #       median 20 / p95 181 points against median 2 / p95 12 on ordinary bars
    #       — one-bar spikes, confined to roll windows. They are kept because the
    #       live path produces them: the alternative is a tape that disagrees with
    #       the deployment at precisely the moment §1 wants parity. The holes in
    #       §0.2 are **not** what this buys — those are closed by the roll instant
    #       alone, and the last-trading-day bar counts are identical without it.
    earlier = ranked["contract"].to_numpy().astype(int) < want.astype(int)
    ranked = ranked[ranked["_front"].to_numpy() | earlier]

    picked = (ranked.sort_values(["datetime", "_front", "volume"], kind="stable")
                    .drop_duplicates(subset="datetime", keep="last"))

    # Tech: keep only minutes this policy's sessions contain, then sort and drop
    #       the working column.
    # Why:  the filter is `classify_session`, so the 1 416 out-of-session bars the
    #       intermediate carries — the 13:45 closing-auction print and the 05:00
    #       boundary — do not reach any tape. They are inert to every consumer
    #       (§0.5: both `run_backtest` and the live aggregator already drop them)
    #       and their only effect was to make raw row counts disagree with
    #       in-session bar counts, which is how `real_data.py` came to print a
    #       split it was not using. Dropping them here is §0.5's "do not
    #       reintroduce it" — note it is a *drop*, never a relabel.
    keep = labels.reindex(picked.index).isin(policy.sessions)
    out = picked.loc[keep].sort_values("datetime", kind="stable")
    n_filled = int((~out["_front"]).sum())
    out = out.drop(columns="_front").reset_index(drop=True)
    out[OHLCV] = out[OHLCV].astype("float64")
    out = out[OUT_COLS]
    out.attrs["filled_from_other_month"] = n_filled
    return out


def _splices(tape: pd.DataFrame) -> int:
    # Tech: count changes in the *running maximum* delivery month, not in the
    #       contract column itself.
    # Why:  a raw change count is not a roll count once the fallback exists. A
    #       filled minute takes the outgoing month for one bar and hands it back,
    #       so the night tape shows 14 498 contract changes against 112 actual
    #       rolls — a number that invites exactly the wrong conclusion next to
    #       §0's table. The running max only ever steps forward, so it counts the
    #       rolls and ignores the excursions.
    forward = tape["contract"].cummax()
    return int((forward != forward.shift()).sum() - 1)


def _dead_contract_bars(tape: pd.DataFrame) -> int:
    # Tech: count bars stamped after their own contract's last trading day 13:30.
    # Why:  T2's acceptance test. `last_trading_day` is the naive third Wednesday
    #       with no holiday table (deliberately — see `roll_at`), so on the four
    #       holiday-shifted expiries it names a date *earlier* than the real one
    #       and the check is strict rather than loose. A zero here is therefore a
    #       real zero; the old day tape scores 1 141 under the same check, all of
    #       it on those four dates.
    cutoff = tape["contract"].map(
        {m: datetime.combine(last_trading_day(m), SETTLEMENT_CLOSE)
         for m in tape["contract"].unique()})
    return int((tape["datetime"] > cutoff).sum())


def _last_trading_day_sessions(tape: pd.DataFrame, session: str) -> pd.Series:
    # Tech: bars per session on each expiry's last trading day.
    # Why:  T3's acceptance test, stated as §0.2 states it. That is the moment the
    #       old rule lost data — it kept the expiring month as "front" for the
    #       whole trading date although it had stopped printing at 13:30 — so a
    #       full 300 (DAY) or 840 (NIGHT) here is the direct evidence the holes
    #       are gone, measured on the tape rather than argued from the rule.
    windows = {"DAY": (dtime(8, 45), timedelta(hours=5)),
               "NIGHT": (dtime(15, 0), timedelta(hours=14))}
    start_at, length = windows[session]
    counts = {}
    for month in sorted(tape["contract"].unique()):
        start = datetime.combine(last_trading_day(month), start_at)
        n = int(((tape["datetime"] >= start)
                 & (tape["datetime"] < start + length)).sum())
        if n:
            counts[month] = n
    return pd.Series(counts, dtype="int64")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, required=True,
                    help="TXF_RPT_minute_bycontract.csv from T1")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="build and check in memory; write nothing")
    args = ap.parse_args(argv)

    t0 = _time.time()
    print(f"reading {args.src}", flush=True)
    # Tech: `contract` is read as text, never inferred.
    # Why:  `load_bar_cache`'s reason — a delivery month is an identifier that
    #       looks like a number, and `202608` typed as int64 is a different key
    #       to the splice rule that compares it against a string.
    bars = pd.read_csv(args.src, parse_dates=["datetime"], dtype={"contract": str})
    labels = session_labels(bars["datetime"])
    print(f"  {len(bars):,} bars, {bars.contract.nunique()} contracts, "
          f"{bars.datetime.min()} .. {bars.datetime.max()}  "
          f"({_time.time() - t0:.0f}s)", flush=True)

    built = {}
    for name, factory, start in TAPES:
        policy = factory()
        tape = front_month_tape(bars, labels, policy)
        if start is not None:
            # Tech: truncate after the front-month selection, not before.
            # Why:  the selection is per-minute and stateless, so the rows are the
            #       same either way — but doing it here makes the tape provably a
            #       suffix of the untruncated one, which the check below asserts.
            #       A start date that silently changed which contract a minute
            #       carries would be a second splice rule hiding in a filter.
            tape = tape[tape["datetime"] >= start].reset_index(drop=True)
        built[name] = tape
        print(f"\n{name}  [{policy.roll_at_time().strftime('%H:%M')}, "
              f"{'+'.join(sorted(policy.sessions))}"
              + (f", from {start:%Y-%m-%d}" if start is not None else "") + "]")
        print(f"  bars      : {len(tape):,}")
        print(f"  span      : {tape.datetime.min()} .. {tape.datetime.max()}")
        print(f"  contracts : {tape.contract.nunique()}   "
              f"splices: {_splices(tape)}")
        print(f"  filled from the other month: "
              f"{tape.attrs['filled_from_other_month']:,}")
        print(f"  bars on a dead contract    : {_dead_contract_bars(tape)}",
              flush=True)

    # Tech: the split tapes' rows, concatenated, against the combined tape —
    #       reported as three numbers: minutes only one side has, and, on the
    #       minutes both have, how many carry a different delivery month.
    # Why:  T2's reconciliation, stated so it is falsifiable rather than a totals
    #       check. §2.3 predicts the disagreement is confined to 15:00–15:29 on a
    #       roll date, where a full-day deployment has already rolled at 12:45 and
    #       the night one has not: any difference *outside* that window is a bug.
    #       Both kinds of disagreement live there and both are correct.
    #
    #       MEASURED 2026-09-01: 3 143 bars over 108 dates hold a different month,
    #       and **8** minutes exist in the combined tape alone — 15:04–15:26 on
    #       eight roll Tuesdays where only the *incoming* month printed. The
    #       combined tape is already on it and keeps the bar; the night tape is
    #       still on the outgoing month, which printed nothing, and the earlier-
    #       only fallback correctly refuses to substitute a later one. That is the
    #       night deployment's real position at that instant, so the two tapes
    #       differing by 8 thin bars is the rule working, not a leak. Nothing ever
    #       goes the other way.
    day, night = built["TXF_RPT_minute_day.csv"], built["TXF_RPT_minute_night.csv"]
    combined = built["TXF_RPT_minute.csv"]
    split = pd.concat([day, night]).sort_values("datetime").reset_index(drop=True)
    print(f"\nreconcile: day {len(day):,} + night {len(night):,} "
          f"= {len(split):,} vs combined {len(combined):,}")
    merged = split.merge(combined, on="datetime", how="outer",
                         suffixes=("_split", "_combined"), indicator=True)
    for side, label in (("right_only", "combined only"), ("left_only", "day+night only")):
        at = merged.loc[merged["_merge"] == side, "datetime"]
        extra = (f", {at.dt.strftime('%H:%M').min()}-{at.dt.strftime('%H:%M').max()} "
                 f"on {at.dt.date.nunique()} dates" if len(at) else "")
        print(f"  minutes in {label:>14}: {len(at):,}{extra}")
    both = merged[merged["_merge"] == "both"]
    differs = both["contract_split"].to_numpy() != both["contract_combined"].to_numpy()
    at = both.loc[differs, "datetime"]
    print(f"  different delivery month : {int(differs.sum()):,}"
          + (f", {at.dt.strftime('%H:%M').min()}-{at.dt.strftime('%H:%M').max()} "
             f"on {at.dt.date.nunique()} dates" if len(at) else ""))

    # Tech: the full-day tape must be the combined tape's suffix, bar for bar.
    # Why:  it exists to answer "where can a full-day deployment be tuned", and
    #       that is a question about the *window*, not about the series. If the
    #       two ever disagreed on a shared minute, the start date would have
    #       become a second splice rule — and a tape tuned on one convention then
    #       deployed against another is the whole failure this rebuild is about.
    fullday = built["TXF_RPT_minute_fullday.csv"]
    suffix = combined[combined["datetime"] >= FULLDAY_START].reset_index(drop=True)
    print(f"\nfullday tape == combined tape from {FULLDAY_START:%Y-%m-%d}: "
          f"{fullday.equals(suffix)}   {len(fullday):,} bars "
          f"({len(fullday) / len(combined):.1%} of the combined tape), "
          f"{fullday.datetime.min()} .. {fullday.datetime.max()}")

    for name, session in (("TXF_RPT_minute_day.csv", "DAY"),
                          ("TXF_RPT_minute_night.csv", "NIGHT")):
        counts = _last_trading_day_sessions(built[name], session)
        full = 300 if session == "DAY" else 840
        print(f"\n{session} on the {len(counts)} last-trading-day sessions "
              f"(a full one is {full}): min {counts.min()}, "
              f"median {counts.median():.0f}, max {counts.max()}, "
              f"short by {int((full - counts).clip(lower=0).sum()):,} bars")

    if args.dry_run:
        print(f"\ndry run — nothing written ({_time.time() - t0:.0f}s)")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, tape in built.items():
        path = args.out_dir / name
        # Tech: CRLF, exactly as the shipped tapes are written.
        # Why:  `dataset/NOTE.md` records all four files as CRLF, and T4 diffs the
        #       new tape against the pre-rebuild copy. A line-ending change would
        #       make every row differ and drown the three causes T4 has to
        #       attribute separately (§3.1).
        tape.to_csv(path, index=False, lineterminator="\r\n")
        print(f"wrote {path}  {path.stat().st_size:,} bytes")
    print(f"done in {_time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
