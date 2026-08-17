"""V1c — does `api.kbars` match the bar convention the model was evaluated on?

Read-only. Logs in, fetches history, prints a verdict. **Places no orders and
subscribes to nothing.**

Why this check exists: every process start backfills 3 008 bars from `kbars`
(§7.5), so if that source labels bars differently, fills empty minutes, or
resolves a different contract, the model's context is wrong at exactly the moment
it is least observable — the first forecasts after every restart. V1 validates
the *tick* path; nothing validates this one.

Four questions, each answerable only against the live API:

  1. **Span** — will one request return ~5 trading days of 1-min bars, or is it
     capped? Determines whether backfill needs paging.
  2. **Label** — left or right edge? The RPT file is left-labelled, so a day
     session (08:45–13:45) runs 08:45 … 13:44. Right-labelling would show 08:46
     … 13:45 instead.
  3. **Empty minutes** — omitted (RPT behaviour) or present as zero-volume
     filler?
  4. **Content** — do the closes match `TXF_RPT_minute_day.csv` bar for bar?

It also brackets the fetch with `api.usage()` to measure the real traffic cost of
a backfill, replacing the ~1 MB estimate in §2.8b with a number.

Usage (credentials from the environment, never from a file in the repo):

    $env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
    uv run python -m forecast_eval.live.v1c_check --date 2026-05-14
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.live.backfill import kbars_to_bars          # noqa: E402
from forecast_eval.live.contracts import txf_root              # noqa: E402
from forecast_eval.real_data import load_minute_bars           # noqa: E402

DEFAULT_RPT = Path(r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv")

# Tech: the day session's first and last left-labelled minutes.
# Why:  the session runs 08:45–13:45, so left-labelling puts the last bar at
#       13:44 and right-labelling at 13:45 — comparing the observed edges against
#       these two constants is the whole label test.
DAY_FIRST_LEFT = "08:45"
DAY_LAST_LEFT = "13:44"


def _resolve_contract(container, name: str):
    # Tech: try attribute access then item access, swallowing KeyError as well as
    #       AttributeError, and return None if neither works.
    # Why:  MEASURED — `getattr(container, name, default)` does NOT protect here:
    #       the Contracts container implements `__getattr__` as a dict lookup that
    #       raises **KeyError**, and getattr's default only suppresses
    #       AttributeError. The three-argument form therefore propagates and
    #       crashes. Both lookup styles are attempted because aliases (`TXFR1`)
    #       and explicit months are not necessarily reachable the same way.
    for access in (lambda: getattr(container, name), lambda: container[name]):
        try:
            found = access()
        except (AttributeError, KeyError, TypeError):
            continue
        if found is not None:
            return found
    return None


def _contract_codes(container):
    # Tech: yield (code, delivery_month) for every contract in the container,
    #       falling back to attribute names when it is not iterable.
    # Why:  the concrete container type is unverified, so enumeration has to be
    #       duck-typed; showing delivery_month alongside the code is what makes
    #       the output answer "is the expired May contract still listed?".
    try:
        items = list(container)
    except TypeError:
        items = [getattr(container, k) for k in dir(container)
                 if not k.startswith("_")]
    out = []
    for c in items:
        code = getattr(c, "code", None)
        if code is None:
            continue
        out.append((str(code), str(getattr(c, "delivery_month", "?"))))
    return sorted(set(out))


def _usage_bytes(api) -> int | None:
    try:
        u = api.usage()
    except Exception as exc:
        print(f"  ! api.usage() failed: {exc!r}")
        return None
    used = getattr(u, "bytes", None)
    remaining = getattr(u, "remaining_bytes", None)
    limit = getattr(u, "limit_bytes", None)
    print(f"  usage: bytes={used} remaining={remaining} limit={limit}")
    return used


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=None,
                    help="A past trading date to compare, YYYY-MM-DD")
    ap.add_argument("--list-contracts", action="store_true",
                    help="List every TXF contract the API offers, then exit. "
                         "Shows whether expired months are still available.")
    ap.add_argument("--span-days", type=int, default=10,
                    help="Calendar days to request in the span test")
    ap.add_argument("--rpt", type=Path, default=DEFAULT_RPT)
    ap.add_argument("--contract", default="TXFR1")
    args = ap.parse_args(argv)

    if not args.date and not args.list_contracts:
        print("ERROR: --date is required (or use --list-contracts)",
              file=sys.stderr)
        return 2

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY in the "
              "environment (never commit them).", file=sys.stderr)
        return 2

    import shioaji as sj

    # Tech: simulation login, then resolve the futures contract by name.
    # Why:  simulation because this check must never touch a production account,
    #       even read-only — the whole point is that it is safe to run casually.
    print(f"logging in (simulation=True) ...")
    api = sj.Shioaji(simulation=True)
    api.login(api_key=api_key, secret_key=secret_key)
    try:
        # Tech: resolve by attribute, falling back to item access.
        # Why:  `TXFR1` is an alias attribute, while an explicit historical
        #       contract (`TXF202605`) is reached by key — supporting both lets a
        #       run compare against the contract that was actually front month on
        #       the target date.
        txf, route = txf_root(api)
        print(f"  contracts reached via {route}")

        if args.list_contracts:
            # Tech: dump every TXF contract the API is offering, then stop.
            # Why:  answers two questions at once that guessing cannot — the key
            #       format for an explicit month, and whether *expired* contracts
            #       are listed at all. If May 2026 is absent here, no historical
            #       diff against an expired month is possible via this route.
            print("\n[contracts] every TXF contract the API lists:")
            for code, month in _contract_codes(txf):
                print(f"  code={code:12s} delivery_month={month}")
            return 0

        contract = _resolve_contract(txf, args.contract)
        if contract is None:
            print(f"ERROR: contract {args.contract!r} not found.\n"
                  f"Available TXF contracts:", file=sys.stderr)
            for code, month in _contract_codes(txf):
                print(f"  code={code:12s} delivery_month={month}",
                      file=sys.stderr)
            return 2
        print(f"  contract {args.contract}: code={getattr(contract, 'code', '?')} "
              f"delivery_month={getattr(contract, 'delivery_month', '?')}")

        # ---- 1. span + traffic cost -------------------------------------
        print("\n[1] span & traffic")
        before = _usage_bytes(api)
        end = datetime.strptime(args.date, "%Y-%m-%d").date()
        start = end - timedelta(days=args.span_days)
        kb = api.kbars(contract, start=start.isoformat(), end=end.isoformat())
        bars = kbars_to_bars(kb)
        after = _usage_bytes(api)
        print(f"  requested {start} .. {end} ({args.span_days} calendar days)")
        print(f"  returned  {len(bars)} bars "
              f"[{bars['timestamp'].min()} .. {bars['timestamp'].max()}]")
        if before is not None and after is not None:
            print(f"  --> BACKFILL COST: {after - before} bytes "
                  f"for {len(bars)} bars")
        print(f"  --> {'OK' if len(bars) >= 3008 else 'TOO FEW'}: "
              f"need >=3008 bars for one context window")

        # ---- 2. label convention ----------------------------------------
        # kbars returns the day AND night sessions (00:00-23:59), so the edges
        # of a calendar date say nothing about labelling. Restrict to the day
        # session window before reading them.
        print("\n[2] label convention")
        # source_label="left" = do NOT shift. This section exists to *measure*
        # the labelling, so it must see the API's raw stamps; applying the
        # corrective shift here would make a right-labelled feed report itself as
        # left-labelled and permanently hide the very thing being checked.
        raw = kbars_to_bars(kb, drop_zero_volume=False, source_label="left")
        same_day = raw[raw["timestamp"].dt.date == end]
        tod = same_day["timestamp"].dt.strftime("%H:%M")
        day = same_day[(tod >= "08:45") & (tod <= "13:46")]
        print(f"  bars on {end}: {len(same_day)} total, {len(day)} in the "
              f"08:45-13:46 window")
        if day.empty:
            print(f"  ! no day-session bars on {end}; pick a trading date")
        else:
            times = day["timestamp"].dt.strftime("%H:%M")
            first, last = times.iloc[0], times.iloc[-1]
            print(f"  first={first} last={last}  (day session 08:45-13:45)")
            if first == "08:46" and last == "13:45":
                print("  --> RIGHT-labelled, as measured on 2026-08-07. "
                      "backfill.kbars_to_bars(source_label='right') shifts "
                      "every bar back one minute. EXPECTED — no action.")
            elif first == DAY_FIRST_LEFT and last in (DAY_LAST_LEFT, "13:45"):
                print("  --> LEFT-labelled. This CONTRADICTS the 2026-08-07 "
                      "measurement — the API changed, or the contract/date is "
                      "unusual. The shift in backfill.py would now be WRONG; "
                      "investigate before running live.")
            else:
                print("  --> UNEXPECTED edges; inspect before trusting backfill.")

        # ---- 3. empty minutes -------------------------------------------
        print("\n[3] empty minutes")
        if "Volume" in pd.DataFrame(dict(kb)).columns:
            zero = int((pd.DataFrame(dict(kb))["Volume"] == 0).sum())
            print(f"  zero-volume rows in payload: {zero}")
            print("  --> " + ("OMITTED like RPT. OK" if zero == 0 else
                              f"kbars FILLS empty minutes ({zero} rows); "
                              "kbars_to_bars drops them — confirm that is right"))
        else:
            print("  ! payload has no Volume column")

        # ---- 4. content vs the RPT file ---------------------------------
        # Compare like with like: the RPT day file is day-session only, and the
        # contract must be the one that was front month on `end` — TXFR1 points
        # at whatever is front month *today*, which is a different contract for
        # any date more than a month back.
        print("\n[4] closes vs TXF_RPT_minute_day.csv")
        if not args.rpt.exists():
            print(f"  ! RPT file not found at {args.rpt}; skipping")
        else:
            rpt_all = pd.read_csv(args.rpt, parse_dates=["datetime"])
            rpt_day = rpt_all[rpt_all["datetime"].dt.date == end]
            if rpt_day.empty:
                # Tech: bail out with the file's coverage rather than computing a
                #       0-of-0 match rate.
                # Why:  a recent date is the right choice for the *label* test but
                #       the RPT file stops earlier, so this branch is expected —
                #       reporting it as a FAIL would be actively misleading.
                last = rpt_all["datetime"].max()
                print(f"  ! RPT has no data for {end} (file ends {last}). "
                      f"Content diff SKIPPED — this is expected on a recent "
                      f"date; sections [1]-[3] above are still valid.")
                print(f"  For a like-for-like content diff, re-run with a date "
                      f"the file covers AND the contract that was front month "
                      f"then, e.g. --date 2026-05-14 --contract TXF202605")
                raise SystemExit(0)
            rpt_contracts = sorted(set(rpt_day["contract"].astype(str))) or ["?"]
            kb_month = str(getattr(contract, "delivery_month", "?"))
            print(f"  RPT contract on {end}: {rpt_contracts}   "
                  f"kbars contract: {kb_month}")
            if kb_month not in rpt_contracts:
                print("  --> CONTRACT MISMATCH: these are different futures. "
                      "Any close difference below is expected and says nothing "
                      "about labelling. Re-run with a recent --date (so TXFR1 "
                      "is the same contract), or pass the historical contract "
                      "explicitly via --contract.")

            rpt = load_minute_bars(args.rpt, None)
            rpt = rpt[rpt["timestamp"].dt.date == end]
            got = bars[bars["timestamp"].dt.date == end]
            got_tod = got["timestamp"].dt.strftime("%H:%M")
            got = got[(got_tod >= "08:45") & (got_tod <= "13:46")]
            merged = rpt.merge(got, on="timestamp", how="outer",
                               suffixes=("_rpt", "_kbars"), indicator=True)
            both = merged[merged["_merge"] == "both"]
            same = (both["price_rpt"] == both["price_kbars"]).sum()
            print(f"  rpt={len(rpt)} kbars={len(got)} common={len(both)} "
                  f"closes equal={same}")
            only_rpt = int((merged["_merge"] == "left_only").sum())
            only_kb = int((merged["_merge"] == "right_only").sum())
            print(f"  minutes only in RPT={only_rpt}  only in kbars={only_kb}")
            rate = same / len(both) if len(both) else 0.0
            print(f"  --> match rate {rate:.4%} "
                  f"({'PASS' if rate >= 0.995 and only_rpt == 0 and only_kb == 0 else 'FAIL'}"
                  f", need >=99.5% and no unmatched minutes)")
    finally:
        # Tech: always log out.
        # Why:  connections are capped at 5 per person_id (§2.8b); leaking one per
        #       aborted run would eventually lock the account out of its own API.
        try:
            api.logout()
            print("\nlogged out.")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
