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
uv run --no-sync python -m forecast_eval.tools.session_artifacts outputs\live_20260824_150112
```
"""
from __future__ import annotations

import argparse
import ast
import json
import re
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


# Tech: the shape of `LiveConfig.describe()`, which is what the recorder's `meta`
#       header carries as its `config` field.
# Why:  a live run writes `params.json` at start-up, so its report opens with a
#       Settings block; a historical directory has no such file and its rebuilt
#       report opened straight into the charts instead — the one structural
#       difference between a rebuilt report and a live one. `describe()` is the
#       only place the run recorded what it was configured as, so this parses it
#       back. Matching the summary against `CONFIGS` would be exact but useless
#       here: the runs that need rebuilding are precisely the ones whose config
#       the registry has since moved past (`e44a047 calmar=13.6742` on
#       `live_20260824_150112` against today's `4282d33 calmar=3.6006`), so the
#       lookup would miss exactly when it is needed.
_DESCRIBE_RE = re.compile(
    r"LiveConfig\[(?P<source_commit>\S+) calmar=(?P<source_calmar>\S+) "
    r"(?P<sessions>\[[^\]]*\])\] "
    r"ckpt=(?P<checkpoint>\S+) ctx=(?P<context_length>\d+) "
    r"stride=(?P<stride_bars>\d+) horizon=(?P<horizon_bars>\d+) "
    r"band=\u00b1(?P<band>\S+) min_prob=(?P<min_prob>\S+) "
    r"trend=(?P<trend>\S+) path=(?P<path>\S+) "
    r"aggr=(?P<aggression_ticks>-?\d+) maxpos=(?P<max_position>\d+) "
    r"forced_close=(?P<forced_close>\S+)$"
)


def params_from_meta(records: List[dict], run_dir: Path) -> Optional[dict]:
    """A `params.json` payload for a historical run, from its `meta` header.

    Returns None when there is no meta record or its config line does not parse.
    """
    # Tech: find the meta record, parse its `config` summary, and assemble the
    #       same dict `live/artifacts.live_params` builds — same keys, same order,
    #       with None for the three the summary line does not carry.
    # Why:  `reports.generate_report` reads `params.json` for its Settings block,
    #       so writing this is what makes a rebuilt report open the way a live one
    #       does. Keeping the *key set* identical is what keeps a rebuilt run
    #       diffable against a live one, which is the property `live_params` says
    #       its shared keys exist for; None reads as "the run did not record
    #       this", which is true, where a plausible-looking guess — today's
    #       dataset path, the default warm-up — would not be. Key order matters
    #       only because the Settings table renders in dict order, and the two
    #       reports are meant to be read side by side.
    meta = next((r for r in records if r.get("event") == "meta"), None)
    if meta is None:
        return None
    m = _DESCRIBE_RE.match(str(meta.get("config", "")))
    if m is None:
        return None

    # Tech: sessions come back as the repr of a sorted list, so literal_eval is
    #       the reader; the policy is rebuilt from them only to recover `roll_at`.
    # Why:  `roll_at_time()` is a pure function of the session set (any policy
    #       containing DAY rolls at 12:45, NIGHT alone at 15:30), so this is
    #       recovered exactly rather than assumed — see `live/sessions.py`.
    from forecast_eval.live.sessions import SessionPolicy
    sessions = list(ast.literal_eval(m.group("sessions")))
    band = float(m.group("band"))

    return {
        "data": "live",
        "source_commit": m.group("source_commit"),
        "source_calmar": float(m.group("source_calmar")),
        "source_dataset": None,
        "checkpoint": m.group("checkpoint"),
        "context_length": int(m.group("context_length")),
        "horizon_bars": int(m.group("horizon_bars")),
        "stride_bars": int(m.group("stride_bars")),
        # Tech: `describe()` prints one `band=±x`, so the sell side is the
        #       negation of the buy side.
        # Why:  an asymmetric band would not survive this round trip, and the
        #       summary line is where that information was lost, not here. Both
        #       pinned configs are symmetric, and the ± notation is the config's
        #       own claim that they are meant to be.
        "buy_threshold": band,
        "sell_threshold": -band,
        "aggression_ticks": int(m.group("aggression_ticks")),
        "min_prob": None if m.group("min_prob") == "None" else float(m.group("min_prob")),
        "max_aggression_ticks": None,
        "max_position": int(m.group("max_position")),
        "forced_close": m.group("forced_close") == "True",
        "sessions": sessions,
        # Why:  `slug` is its own `meta` field (since 2026-10-27), like the
        #       provenance keys below; an older session re-derives it.
        "slug": meta.get("slug") or SessionPolicy.of(sessions).cache_slug(),
        "roll_at": str(SessionPolicy.of(sessions).roll_at_time()),
        "broker": meta.get("broker"),
        # Tech: the five provenance keys, read from the `meta` header rather than
        #       from the `config` summary line, with None/{} when a session
        #       predates them.
        # Why:  LIVE_STRATEGY_ROUTING_PLAN Phase 5 added them to `live_params`,
        #       and `test_the_rebuilt_params_are_the_live_ones_key_for_key` holds
        #       the two key sets identical. They are recorded as their own `meta`
        #       fields precisely so they survive this round trip -- packing them
        #       into `describe()` would have meant extending `_DESCRIBE_RE` and
        #       breaking every session file already on disk.
        "strategy_package": meta.get("strategy_package"),
        "strategy_sha": meta.get("strategy_sha"),
        "git_commit": meta.get("git_commit"),
        "git_dirty": meta.get("git_dirty"),
        "strategy_origins": meta.get("strategy_origins") or {},
        "strategy_overridden": any(
            v == "param" for v in (meta.get("strategy_origins") or {}).values()),
        "warmup_days": None,
        "soak_id": meta.get("soak_id"),
        "output_dir": str(run_dir),
    }


def load_price_series(run_dir: Path) -> Optional[pd.DataFrame]:
    """`ticks.csv` as the `(timestamp, price)` frame the report charts against."""
    # Tech: delegate to `live/capture.py`, which owns the capture format.
    # Why:  this used to re-read the CSV itself, and in doing so rediscovered the
    #       mixed ISO-8601 shapes that `capture.read_ticks` had already handled
    #       and documented (§2.17). One reader for one format; a second copy is
    #       how the two drift.
    from forecast_eval.live.capture import price_series
    return price_series(run_dir / "ticks.csv")


def seed_ledger(result: BacktestResult, run_dir: Path, *, config: str,
                soak_id: Optional[str] = None) -> int:
    """Append a finished run's fills to the deployment's ledger, for `--resume`.

    Returns the number appended, or -1 when it refuses.
    """
    # Tech: resolve the deployment's ledger and soak id, refuse if rows for that
    #       soak are already there, then append.
    # Why:  this is a MIGRATION and not part of the ordinary path — a live run
    #       keeps its own ledger current as it goes. It exists because the
    #       position a soak holds today can predate the ledger entirely, and then
    #       `--resume` has nothing to replay, falls back to the synthetic fill,
    #       and loses a history that is sitting in a log on disk.
    #
    #       The idempotence guard is not optional. Appending twice would double
    #       every fill, which breaks the reconciliation in `cli.py` — and that
    #       check is the only thing standing between a wrong ledger and a
    #       confidently wrong `trades.csv`.
    from forecast_eval.live.config import config_named
    from forecast_eval.live.ledger import FillLedger, ledger_path, net_position

    cfg = config_named(config)
    if soak_id is None:
        epoch = run_dir.parent / "soak_epoch.json"
        if not epoch.exists():
            print(f"ERROR: no {epoch} — pass --soak-id explicitly", file=sys.stderr)
            return -1
        soak_id = json.loads(epoch.read_text(encoding="utf-8"))["soak_id"]

    path = ledger_path(run_dir.parent, cfg.policy)
    if FillLedger(path, soak_id=soak_id).replay():
        print(f"ERROR: {path} already has rows for {soak_id}. Appending again "
              f"would double every fill and break the reconciliation in cli.py.",
              file=sys.stderr)
        return -1

    added = FillLedger(path, soak_id=soak_id).sync(result.fills)
    print(f"  ledger       {path}")
    print(f"               +{added} fill(s) under {soak_id}, "
          f"net {net_position(result.fills):+d}")
    print(f"               a --resume on --config {config} replays these; cli.py "
          f"refuses them unless the venue agrees with that net")
    return added


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="overwrite artifacts that are already there")
    ap.add_argument("--seed-ledger", action="store_true",
                    help="append this run's fills to the deployment's ledger, "
                         "so a later --resume replays them")
    ap.add_argument("--config", default="night",
                    help="which deployment's ledger to seed (default: night)")
    ap.add_argument("--soak-id", default=None,
                    help="soak to tag the rows with (default: the epoch on disk)")
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

    # Tech: synthesise `params.json` from the meta header, but only when the run
    #       did not write one itself.
    # Why:  it is what gives the rebuilt report the Settings block a live run's
    #       report opens with — the one section a rebuilt directory was missing.
    #       Never over an existing file: that one came from the run's own
    #       `LiveConfig` and is complete, where this is a reconstruction from a
    #       one-line summary, and `--force` is about the artifacts the loop
    #       derives, not about the run's own record of what it was.
    params = None
    if (args.run_dir / "params.json").exists():
        print("  params.json already there — left alone")
    else:
        params = params_from_meta(records, args.run_dir)
        if params is None:
            print("  WARNING: the meta header carries no parseable config line, "
                  "so the report opens without its Settings block", file=sys.stderr)

    written = write_all(result, args.run_dir, ext=".csv", params=params,
                        auto_report=not args.no_report, report_ticks=ticks)
    for name, path in sorted(written.items()):
        print(f"  {name:12} {path}")

    if args.seed_ledger and seed_ledger(result, args.run_dir, config=args.config,
                                        soak_id=args.soak_id) < 0:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
