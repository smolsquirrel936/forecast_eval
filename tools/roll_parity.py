"""TAPE_REBUILD_PLAN §0.5 — the tape-spanning parity check, across every roll.

§0.5 separates two claims that are both called "parity":

  * the **transcription** parity `DAY_SESSION_PLAN.md` W7 delivers — `LiveLoop`
    decides what `run_backtest` decides, *given the same bars in*. It is done,
    and it is deliberately synthetic: both sides are handed one stream, so
    nothing in it can notice that the two sides would not have been handed the
    same stream in the first place.
  * the **tape-spanning** parity §1 describes — the evaluated series and the
    series the deployment would really have held, compared across a roll. §0.5
    records it as "has not been done, and cannot pass today", because before the
    rebuild the tape spliced 1 d 8 h 30 m after the live rule and had holes the
    live path structurally cannot have.

This module is the second one. It answers, over real data rather than a fixture:

    at each roll, is the shipped tape the same series the live warm-up path
    would have built from the same prints?

**Both sides come from `TXF_RPT_minute_bycontract.csv`, and only one of them is
the thing under test.** The tape side is read from the shipped file — it is what
`results.tsv`'s numbers were measured on. The live side is rebuilt here by
calling `live/backfill.collapse_front_month` itself, with the `front_month_at`
`live/cli.py` really passes it — **bound to the deployment's policy, tracking
that call site**, which is where §5.3's defect was — over the months
`_warmup_contracts` can really reach. Nothing restates either rule: a check
written by copying the rule it checks proves only that the copy was faithful.

**What it cannot see.** The venue's `TXFR1` alias rolls at settlement, not at
`roll_at`, so which contracts are *listed* can lag this model on the roll day
itself; that is `RollWatch`'s question (§2.6) and is not modelled here. And the
comparison is windowed around each roll — away from one, every candidate month
maps to the same answer under either rule, so there is nothing there to differ.

```powershell
uv run python -m forecast_eval.tools.roll_parity --deployment night
uv run python -m forecast_eval.tools.roll_parity --deployment day --out outputs\roll_parity_day.csv
```
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import timedelta
from functools import partial
from pathlib import Path

import pandas as pd

from forecast_eval.live.backfill import collapse_front_month
from forecast_eval.live.roll import front_month_at, roll_calendar
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.tools.build_session_tapes import session_labels

DATASET = Path(r"\\DESKTOP-SQUIRRE\dataset\RPT")
BYCONTRACT = DATASET / "TXF_RPT_minute_bycontract.csv"

# Tech: the four shipped tapes, as (filename, policy factory).
# Why:  the same pairing `build_session_tapes.TAPES` carries, for the same reason
#       — the splice instant lives on the policy (12:45 for day and full-day,
#       15:30 for night) and is not guessable from the filename. Restated rather
#       than imported because that table also carries `FULLDAY_START`, which is a
#       build-time concern and would only be a third thing to explain here.
TAPES = {
    "day": ("TXF_RPT_minute_day.csv", SessionPolicy.day),
    "night": ("TXF_RPT_minute_night.csv", SessionPolicy.night),
    "combined": ("TXF_RPT_minute.csv", SessionPolicy.both),
    "fullday": ("TXF_RPT_minute_fullday.csv", SessionPolicy.both),
}

READ_COLS = ["datetime", "contract", "close", "volume"]
CHUNK = 1_000_000


def roll_windows(first: pd.Timestamp, last: pd.Timestamp, policy, hours: float):
    """Every roll instant in `[first, last]`, with a +-`hours` window around it.

    Returns a frame of `(month, roll_at, start, end)`, ascending.
    """
    # Tech: ask `roll_calendar` for the boundaries the *tape* was spliced on, and
    #       keep those inside the tape's own span.
    # Why:  the roll instants are the whole content of this check, and there is
    #       exactly one function that knows them (TAPE_REBUILD_PLAN T0 made the
    #       tape builder and the live path share it). Deriving them here from
    #       third Wednesdays would reintroduce the second rule the rebuild
    #       removed.
    bounds, months = roll_calendar(f"{first.year}{first.month:02d}",
                                   f"{last.year}{last.month:02d}", policy)
    span = timedelta(hours=hours)
    rows = [{"month": m, "roll_at": pd.Timestamp(b),
             "start": pd.Timestamp(b) - span, "end": pd.Timestamp(b) + span}
            for m, b in zip(months, bounds)
            if first <= pd.Timestamp(b) <= last]
    return pd.DataFrame(rows)


def read_windows(path: Path, windows: pd.DataFrame) -> pd.DataFrame:
    """Every row of a minute CSV that falls inside one of `windows`."""
    # Tech: stream the file in chunks, keep only rows inside a window, and tag
    #       each survivor with the window it belongs to.
    # Why:  the intermediate is 288 MB / 4.55 M rows and the windows hold well
    #       under 1 % of it. Chunking keeps this runnable beside a model in
    #       memory; the tag is what lets every count below be reported per roll
    #       rather than as one total that hides which roll went wrong.
    starts = windows["start"].to_numpy()
    ends = windows["end"].to_numpy()
    kept = []
    for chunk in pd.read_csv(path, usecols=READ_COLS, dtype={"contract": str},
                             chunksize=CHUNK):
        ts = pd.to_datetime(chunk["datetime"])
        idx = starts.searchsorted(ts.to_numpy(), side="right") - 1
        inside = (idx >= 0) & (ts.to_numpy() <= ends[idx.clip(0)])
        if inside.any():
            got = chunk.loc[inside].assign(datetime=ts[inside],
                                           window=idx[inside])
            kept.append(got)
    if not kept:
        return pd.DataFrame(columns=READ_COLS + ["window"])
    return pd.concat(kept, ignore_index=True)


def live_series(bars: pd.DataFrame, policy) -> pd.DataFrame:
    """The series the live warm-up path would have built from `bars`.

    `bars` is per-`(contract, minute)` OHLCV; the result is one row a minute,
    front-month-continuous, filtered to `policy`'s sessions.
    """
    # Tech: drop months the live path cannot fetch, hand what is left to the real
    #       `collapse_front_month`, then apply the aggregator's session filter.
    # Why:  three separate fidelity claims, and each one is a way this check
    #       could have been written to pass by accident:
    #
    #       (1) REACH. `_warmup_contracts` returns "the listed months up to and
    #           including the front one" — so a minute the front month missed can
    #           be filled only from an *earlier* month, never the far one. The
    #           tape builder carries the same restriction for the same reason; a
    #           live side without it would be given prints the deployment cannot
    #           see and would look better than the deployment is.
    #       (2) THE SPLICE INSTANT. `front_month_at` is bound to **this
    #           deployment's** policy, because that is what `live/cli.py:645`
    #           passes. It passed the function *unbound* until 2026-09-01, so its
    #           splice was the module's `ROLL_AT` — NIGHT's 15:30 — for every
    #           deployment, including a DAY one whose tape spliced at 12:45. That
    #           is the defect this check found (TAPE_REBUILD_PLAN §5.3). The
    #           binding here tracks the call site rather than the correct answer:
    #           point it back at the bare function and the old measurement
    #           reproduces exactly, which is what makes this a check and not an
    #           assertion that the fix was applied.
    #       (3) THE SESSION FILTER. Live, `TickAggregator` drops anything outside
    #           `policy.sessions` before the loop sees it; the tape has the same
    #           minutes dropped at build time. Without this the live side carries
    #           minutes the tape was never going to have and every window reports
    #           spurious extras.
    splice = partial(front_month_at, policy=policy)
    front = pd.Series(splice(t) for t in bars["datetime"])
    reachable = bars["contract"].to_numpy().astype(int) <= front.to_numpy().astype(int)
    bars = bars.loc[reachable]

    frame = pd.DataFrame({"timestamp": bars["datetime"].to_numpy(),
                          "price": bars["close"].to_numpy(),
                          "volume": bars["volume"].to_numpy(),
                          "contract": bars["contract"].to_numpy()})
    merged = collapse_front_month([frame], front_month_at=splice)
    # Tech: re-assert the timestamp dtype before returning.
    # Why:  `collapse_front_month`'s empty path returns bare `BAR_COLUMNS`, whose
    #       `timestamp` is object. A window where the live path produces nothing
    #       is a *result* — the tape has minutes the deployment could not have
    #       built — so it has to reach `compare` and be counted, not raise there.
    merged["timestamp"] = pd.to_datetime(merged["timestamp"])
    if merged.empty:
        return merged
    keep = session_labels(merged["timestamp"]).isin(policy.sessions)
    return merged.loc[keep.to_numpy()].reset_index(drop=True)


def compare(tape: pd.DataFrame, live: pd.DataFrame) -> dict:
    """One window's verdict: how the two series differ, minute by minute."""
    # Tech: outer-join the two on the minute and count the four ways a row can
    #       disagree — present on one side only, different contract, different
    #       close.
    # Why:  a single "n mismatches" would not distinguish the failure the rebuild
    #       was for (a *hole* — a minute the tape lacks entirely, §0.2) from the
    #       one it could still have (the same minute sourced from a different
    #       month, which is a splice-instant disagreement and moves the price by
    #       the inter-month basis, §0.4). They have different causes and different
    #       consequences, so they are counted apart.
    both = tape.merge(live, how="outer", left_on="datetime",
                      right_on="timestamp", suffixes=("_tape", "_live"),
                      indicator=True)
    only_tape = both["_merge"] == "left_only"
    only_live = both["_merge"] == "right_only"
    shared = both["_merge"] == "both"
    diff_contract = shared & (both["contract_tape"].astype(str)
                              != both["contract_live"].astype(str))
    diff_close = shared & (both["close"] != both["price"])
    delta = (both.loc[diff_close, "close"] - both.loc[diff_close, "price"]).abs()
    return {
        "tape_bars": int(len(tape)),
        "live_bars": int(len(live)),
        "only_tape": int(only_tape.sum()),
        "only_live": int(only_live.sum()),
        "diff_contract": int(diff_contract.sum()),
        "diff_close": int(diff_close.sum()),
        "max_abs_dclose": float(delta.max()) if len(delta) else 0.0,
        "median_abs_dclose": float(delta.median()) if len(delta) else 0.0,
        "first_diff": (both.loc[diff_contract | diff_close, "datetime"].min()
                       if (diff_contract | diff_close).any() else pd.NaT),
        "last_diff": (both.loc[diff_contract | diff_close, "datetime"].max()
                      if (diff_contract | diff_close).any() else pd.NaT),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--deployment", choices=sorted(TAPES), default="night")
    ap.add_argument("--tape", type=Path, default=None,
                    help="override the shipped tape for this deployment")
    ap.add_argument("--by-contract", type=Path, default=BYCONTRACT)
    ap.add_argument("--window-hours", type=float, default=24.0,
                    help="half-width of the window compared around each roll")
    ap.add_argument("--out", type=Path, default=None, help="per-roll table as CSV")
    args = ap.parse_args(argv)

    t0 = _time.time()
    name, make_policy = TAPES[args.deployment]
    policy = make_policy()
    tape_path = args.tape or DATASET / name

    # Tech: read the tape once to learn its span, then window both files.
    # Why:  the span has to come from the tape rather than from a hard-coded pair
    #       of dates — the four tapes do not share a start (the full-day one
    #       begins 2018-01-01) and a roll outside a tape's own span would be
    #       compared against nothing and reported as a total loss.
    tape_all = pd.read_csv(tape_path, usecols=READ_COLS, dtype={"contract": str},
                           parse_dates=["datetime"])
    first, last = tape_all["datetime"].iloc[0], tape_all["datetime"].iloc[-1]
    windows = roll_windows(first, last, policy, args.window_hours)
    print(f"{tape_path}\n  {len(tape_all):,} bars, {first} .. {last}")
    print(f"  {len(windows)} rolls in span, +-{args.window_hours:g} h each, "
          f"splice at {policy.roll_at_time()} (live path splices at 15:30)",
          flush=True)

    starts = windows["start"].to_numpy()
    ends = windows["end"].to_numpy()
    idx = starts.searchsorted(tape_all["datetime"].to_numpy(), side="right") - 1
    inside = (idx >= 0) & (tape_all["datetime"].to_numpy() <= ends[idx.clip(0)])
    tape_win = tape_all.loc[inside].assign(window=idx[inside])
    del tape_all

    print(f"  reading {args.by_contract.name} ...", flush=True)
    by_win = read_windows(args.by_contract, windows)
    print(f"  {len(by_win):,} candidate rows in {_time.time() - t0:.0f}s",
          flush=True)

    rows = []
    for w in range(len(windows)):
        got = compare(tape_win[tape_win["window"] == w],
                      live_series(by_win[by_win["window"] == w], policy))
        got.update(month=windows.at[w, "month"], roll_at=windows.at[w, "roll_at"])
        rows.append(got)
    table = pd.DataFrame(rows)

    # Tech: a roll is clean when no minute is missing, extra, or sourced from a
    #       different month than the tape used.
    # Why:  §0.5's question is a yes/no about the series, and the headline has to
    #       be that verdict rather than a mean of it. One dirty roll out of 112 is
    #       not "99 % parity" — it is a deployment that disagrees with its own
    #       backtest twelve times a year.
    dirty = table[(table["only_tape"] > 0) | (table["only_live"] > 0)
                  | (table["diff_contract"] > 0) | (table["diff_close"] > 0)]
    print(f"\n{len(table) - len(dirty)}/{len(table)} rolls identical")
    if len(dirty):
        cols = ["month", "only_tape", "only_live", "diff_contract", "diff_close",
                "max_abs_dclose", "first_diff", "last_diff"]
        print(f"\n{len(dirty)} rolls differ:")
        print(dirty[cols].to_string(index=False))
        print(f"\ntotals: only_tape={int(dirty['only_tape'].sum())} "
              f"only_live={int(dirty['only_live'].sum())} "
              f"diff_contract={int(dirty['diff_contract'].sum())} "
              f"diff_close={int(dirty['diff_close'].sum())}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(args.out, index=False)
        print(f"\nwrote {args.out}")
    print(f"total {_time.time() - t0:.0f}s")
    return 1 if len(dirty) else 0


if __name__ == "__main__":
    sys.exit(main())
