r"""Rebuild a live run's artifacts from the `session.jsonl` it already wrote.

`live/artifacts.py` gives a *running* deployment the backtest's artifact set, and
does it by handing `LiveLoop.result()` to `write_all` — no projection, one writer,
so the schemas cannot drift. That is the right answer for a live run and it is
not available here: these runs are over, their loop objects are gone, and the
only surviving record is the recorder's event stream.

So this module is the projection, deliberately, for **historical run directories
only**. It reads `session.jsonl`, rebuilds the event objects the live loop held,
and hands them to the same `write_all`. The rebuilt lists go through the real
`FillEvent` / `OrderEvent` / `Forecast` types rather than straight to a frame,
which is what keeps this from becoming a second schema: if the events change,
this stops compiling rather than quietly writing the old shape.

**Do not wire this into the live path.** A live run writes its own artifacts as
it goes; re-deriving them from the log would reintroduce exactly the drift the
one-writer rule exists to prevent.

```powershell
uv run python -m forecast_eval.tools.session_artifacts outputs\live_20260824_150112
```
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from forecast_eval.events import FillEvent, Forecast, OrderEvent
from forecast_eval.logging_io import write_all
from forecast_eval.portfolio import Portfolio
from forecast_eval.run import BacktestResult

# Tech: the venue's clock offset from UTC.
# Why:  MEASURED on `live_20260824_150112`, and it is a real inconsistency in the
#       recorder rather than a choice. `bar`, `order` and `signal` records carry
#       naive **local** timestamps ("2026-08-24T20:01:00"), while `fill` records
#       carry a float **epoch** (1787572931.325908) — the same instant, eight
#       hours apart. Converting the epoch without this would put every fill eight
#       hours before its own order, mislabel its session through
#       `classify_session`, and place it off the left edge of every chart.
#       TAIFEX is Asia/Taipei, which is UTC+8 all year with no DST, so the offset
#       is a constant rather than a lookup. `_check_alignment` verifies it against
#       the run's own bars rather than trusting it.
#
#       A `datetime.timedelta` rather than a `pd.Timedelta`: the pandas
#       constructor raises a NumPy "generic unit" DeprecationWarning on
#       this version, and a tool that prints a deprecation notice above
#       its own output teaches the reader to skim past warnings.
VENUE_UTC_OFFSET = timedelta(hours=8)


def read_records(path: Path) -> List[dict]:
    """Every JSON object in a `session.jsonl`, skipping unparseable lines."""
    # Tech: parse line by line, ignoring anything that does not decode.
    # Why:  the recorder flushes per line so the trail survives a kill -9, which
    #       means the final line of a killed run can be a partial write. Dropping
    #       it is the whole handling this needs — every earlier line is intact by
    #       construction.
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except Exception:                          # noqa: BLE001 — see Why
                continue
    return out


def _ts(value: Any) -> pd.Timestamp:
    # Tech: a record timestamp, whether it is an ISO string or a float epoch.
    # Why:  both shapes appear in one file (see `VENUE_UTC_OFFSET`), so the
    #       conversion has to be decided per value rather than per column.
    if isinstance(value, (int, float)):
        return pd.to_datetime(float(value), unit="s") + VENUE_UTC_OFFSET
    return pd.Timestamp(value)


def rebuild(records: List[dict]) -> BacktestResult:
    """The event lists a `LiveLoop` held, from the records it wrote."""
    # Tech: one pass, dispatching on the event name into the four lists that
    #       `write_all` consumes.
    # Why:  built as the real event types, not as dict rows, so this shares the
    #       backtest's schema by construction. `signals` is genuinely a list of
    #       dicts in the loop too, so it is the one that stays untyped — its keys
    #       are the ones `LiveLoop` writes.
    fills: List[FillEvent] = []
    orders: List[OrderEvent] = []
    forecasts: List[Forecast] = []
    signals: List[Dict[str, Any]] = []
    n_bars = 0

    for r in records:
        kind = r.get("event")
        if kind == "bar":
            n_bars += 1
        elif kind == "fill":
            fills.append(FillEvent(
                timestamp=_ts(r["timestamp"]), side=r["side"],
                fill_price=float(r["fill_price"]), quantity=int(r["quantity"]),
                fee=float(r.get("fee", 0.0))))
        elif kind == "order":
            orders.append(OrderEvent(
                timestamp=_ts(r["timestamp"]), side=r["side"],
                limit_price=float(r["limit_price"]),
                intent=r.get("intent", "OPEN"),
                quantity=int(r.get("quantity", 1))))
        elif kind == "forecast":
            forecasts.append(Forecast(
                timestamp=_ts(r["timestamp"]),
                horizon_bars=int(r.get("horizon_bars", 0)),
                payload=r.get("payload") or {}))
        elif kind == "signal":
            signals.append({
                "timestamp": _ts(r["timestamp"]),
                "direction": r.get("direction"),
                "price": r.get("price"),
                "session": r.get("session"),
                "strength": r.get("strength"),
            })

    return BacktestResult(
        portfolio=Portfolio(contract_multiplier=200.0),
        n_bars=n_bars, n_signals=len(signals), n_orders=len(orders),
        n_forecasts=len(forecasts), fills=fills, orders=orders,
        forecasts=forecasts, signals=signals)


def check_alignment(result: BacktestResult, records: List[dict]) -> Optional[str]:
    """Complain when a rebuilt fill lands outside the run's own bars."""
    # Tech: compare every fill timestamp against the first and last `bar`.
    # Why:  this is what makes `VENUE_UTC_OFFSET` a checked assumption rather
    #       than a hopeful constant. A wrong offset is silent in every other
    #       respect — the artifacts still write, the report still renders — and
    #       shows up only as fills drawn nowhere near the price line, which is
    #       exactly the kind of thing a reader would take at face value.
    bars = [_ts(r["timestamp"]) for r in records if r.get("event") == "bar"]
    if not bars or not result.fills:
        return None
    lo, hi = min(bars), max(bars)
    stray = [f.timestamp for f in result.fills if not (lo <= f.timestamp <= hi)]
    if not stray:
        return None
    return (f"{len(stray)} of {len(result.fills)} fills fall outside the run's "
            f"bars ({lo} .. {hi}); first stray {stray[0]}. The clock offset is "
            f"probably wrong — see VENUE_UTC_OFFSET.")


def load_price_series(run_dir: Path) -> Optional[pd.DataFrame]:
    """`ticks.csv` as the `(timestamp, price)` frame the report charts against."""
    # Tech: read the capture, drop simtrade prints, keep two columns.
    # Why:  the report draws the price line and the buy-and-hold curve from this.
    #       Simtrade prints are excluded for the reason §2.5 gives — they are not
    #       evidence the market traded — so a chart built from them would not be
    #       the series the run actually saw.
    path = run_dir / "ticks.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, usecols=["timestamp", "price", "simtrade"])
    df = df[df["simtrade"] == 0]
    # Tech: `format="ISO8601"` rather than letting pandas infer one format.
    # Why:  MEASURED — the capture mixes both ISO shapes in one file, because a
    #       print that lands exactly on a second has no fractional part
    #       ("2026-08-24T15:11:23" at row 320, among 287 726 that do). Inference
    #       locks onto the first row's format and then raises on the first row
    #       that differs, which reads as a corrupt capture rather than a
    #       perfectly ordinary one.
    return pd.DataFrame({"timestamp": pd.to_datetime(df["timestamp"],
                                                     format="ISO8601"),
                         "price": df["price"].astype(float)})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="overwrite artifacts that are already there")
    args = ap.parse_args(argv)

    session = args.run_dir / "session.jsonl"
    if not session.exists():
        print(f"ERROR: {session} not found", file=sys.stderr)
        return 2

    # Tech: refuse to overwrite an existing artifact set unless asked.
    # Why:  a run from `live/artifacts.py` wrote its own, from the loop itself.
    #       Those are the primary record and this is a reconstruction; silently
    #       replacing the first with the second would be a downgrade, and an
    #       invisible one.
    existing = [n for n in ("fills.csv", "trades.csv", "signals.csv")
                if (args.run_dir / n).exists()]
    if existing and not args.force:
        print(f"ERROR: {args.run_dir} already has {existing} — these were "
              f"written by the run itself and are the primary record. "
              f"Pass --force to overwrite.", file=sys.stderr)
        return 3

    records = read_records(session)
    result = rebuild(records)
    print(f"{session}\n  {len(records):,} records -> "
          f"{result.n_bars:,} bars, {len(result.forecasts)} forecasts, "
          f"{len(result.signals)} signals, {len(result.orders)} orders, "
          f"{len(result.fills)} fills")

    warning = check_alignment(result, records)
    if warning:
        print(f"  WARNING: {warning}", file=sys.stderr)

    ticks = load_price_series(args.run_dir)
    if ticks is not None:
        print(f"  {len(ticks):,} non-simtrade prints for the price charts")

    written = write_all(result, args.run_dir, ext=".csv",
                        auto_report=not args.no_report, report_ticks=ticks)
    for name, path in sorted(written.items()):
        print(f"  {name:12} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
