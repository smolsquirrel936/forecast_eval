"""The price line a soak's report is drawn against — every segment, not one run.

`live/artifacts.py` renders a report per run directory, and until now its price
background was that run's own `ticks.csv`. A soak is many runs: a restart opens a
new directory, so the chart restarted with it and showed a few minutes of prices
under fills that span weeks. Worse, a `--resume` replays fills from *earlier*
segments (see [ledger.py](ledger.py)), so the report could plot a fill from a day
the chart had no prices for at all — which is the same "fills drawn nowhere near
the price line" failure `tools/session_artifacts.check_alignment` exists to catch,
arriving from the other direction.

**Two rules decide which captures are in.** A directory belongs if its
`session.jsonl` says it belongs to this soak — or if its capture *covers a fill
being charted*, which is what pulls in the run that opened a position the current
soak inherited. The second rule is not a special case for one directory: any
resumed history has this shape, because the fills come from a run whose prices
live somewhere else.

**Minute resolution, cached.** A night is ~25 000 prints and a soak runs for
months, so the assembled series is downsampled to one row a minute before it is
kept — the report draws at most `n_ticks_for_chart` points anyway, and a minute
bar is the series the strategy actually traded. The result is cached in
`prices_<slug>.csv` beside the fill ledger, keyed by run directory, so each
capture is parsed exactly once no matter how many times a report is rendered.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from .capture import price_series

CACHE_COLUMNS = ["timestamp", "price", "run"]


def cache_path(out_parent: Any, policy: Any) -> Path:
    """`prices_<slug>.csv`, keyed like the bar cache and the fill ledger."""
    return Path(out_parent) / f"prices_{policy.cache_slug()}.csv"


def _soak_of(run_dir: Path) -> Optional[str]:
    # Tech: the `soak_id` from the first line of a run's `session.jsonl`.
    # Why:  the recorder writes `meta` first, so this is one short read rather
    #       than a parse of a file that reaches megabytes. A run that predates the
    #       soak-epoch mechanism has no `soak_id` and answers None, which is
    #       correct rather than exceptional — the two 2026-08 directories are
    #       exactly that.
    session = run_dir / "session.jsonl"
    if not session.exists():
        return None
    try:
        with session.open(encoding="utf-8") as fh:
            return (json.loads(fh.readline()) or {}).get("soak_id")
    except Exception:                                  # noqa: BLE001 — see Why
        return None


def capture_span(path: Path) -> Optional[Tuple[pd.Timestamp, pd.Timestamp]]:
    """The first and last timestamp in a capture, without parsing the middle."""
    # Tech: read the first data row, then seek to the tail for the last one.
    # Why:  inclusion is decided for every sibling directory, and one of them
    #       holds 287 726 prints. Parsing all of them to find out which are
    #       relevant would cost more than the report. The tail read is bounded at
    #       8 KB, which is many rows wide.
    try:
        with path.open("rb") as fh:
            fh.readline()                              # header
            first = fh.readline().decode("utf-8", "ignore")
            if not first.strip():
                return None
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", "ignore").strip().splitlines()
        last = next((ln for ln in reversed(tail) if ln.strip()), None)
        if last is None:
            return None
        lo = pd.Timestamp(first.split(",")[0])
        hi = pd.Timestamp(last.split(",")[0])
        return (lo, hi) if lo <= hi else (hi, lo)
    except Exception:                                  # noqa: BLE001 — see Why
        # Why:  a capture is written by a process that can be killed mid-row, so a
        #       malformed head or tail is ordinary. It only costs this directory's
        #       inclusion, never the render.
        return None


def minute_series(path: Path) -> Optional[pd.DataFrame]:
    """A capture as one `(timestamp, price)` row a minute."""
    # Tech: the full capture through `capture.price_series`, resampled to the
    #       last print of each minute.
    # Why:  the last print of a minute is the same convention the evaluated tape
    #       uses for `close`, so the chart's line is the series the strategy was
    #       measured on rather than a different summary of the same ticks.
    df = price_series(path)
    if df is None or df.empty:
        return None
    out = (df.set_index("timestamp")["price"].resample("1min").last().dropna())
    return out.reset_index()


def _candidates(out_parent: Path, soak_id: Optional[str],
                cover: Sequence[pd.Timestamp]) -> List[Path]:
    # Tech: every sibling run directory that either belongs to this soak or has a
    #       capture spanning one of `cover`.
    # Why:  the second clause is what makes a *resumed* report coherent. The
    #       fills replayed from the ledger were executed in an earlier run, whose
    #       prices are in that run's directory and nowhere else; without it the
    #       chart plots them against a line that does not reach them.
    dirs = []
    for d in sorted(p for p in out_parent.iterdir() if p.is_dir()):
        capture = d / "ticks.csv"
        if not capture.exists():
            continue
        if soak_id is not None and _soak_of(d) == soak_id:
            dirs.append(d)
            continue
        if not cover:
            continue
        span = capture_span(capture)
        if span and any(span[0] <= t <= span[1] for t in cover):
            dirs.append(d)
    return dirs


def soak_price_series(out_parent: Any, *, policy: Any,
                      soak_id: Optional[str] = None,
                      cover: Iterable[Any] = (),
                      run_dir: Optional[Any] = None) -> Optional[pd.DataFrame]:
    """The `(timestamp, price)` line spanning this soak, cached across renders."""
    # Tech: read the cache, parse only the run directories it does not already
    #       hold, append them, and return the union sorted by time.
    # Why:  the cache is keyed by run directory *name*, so a finished segment is
    #       parsed exactly once ever — which is what keeps a three-month soak's
    #       report from re-reading gigabytes of captures every time it renders.
    #       The live segment is deliberately NOT cached: it is still growing, so
    #       its rows would go stale the moment they were written.
    out_parent = Path(out_parent)
    cover = [pd.Timestamp(t) for t in cover]
    path = cache_path(out_parent, policy)

    cached = pd.DataFrame(columns=CACHE_COLUMNS)
    if path.exists():
        try:
            cached = pd.read_csv(path, parse_dates=["timestamp"])
        except Exception:                              # noqa: BLE001 — see Why
            # Why:  the same rule as every other file here — a corrupt cache costs
            #       a re-parse, never a render.
            cached = pd.DataFrame(columns=CACHE_COLUMNS)

    live_name = Path(run_dir).name if run_dir is not None else None
    have = set(cached["run"].astype(str)) if not cached.empty else set()
    fresh = []
    for d in _candidates(out_parent, soak_id, cover):
        if d.name in have or d.name == live_name:
            continue
        got = minute_series(d / "ticks.csv")
        if got is not None and not got.empty:
            fresh.append(got.assign(run=d.name))

    if fresh:
        # Tech: an empty cache is left out of the concat rather than concatenated
        #       as an empty frame.
        # Why:  pandas warns that concatenating empty or all-NA frames will stop
        #       contributing dtypes in a future version, and the first render of
        #       any soak hits exactly that path — so the warning would greet every
        #       new deployment on its first report.
        parts = ([cached] if not cached.empty else []) + fresh
        cached = pd.concat(parts, ignore_index=True)
        try:
            cached.to_csv(path, index=False)
        except Exception:                              # noqa: BLE001 — see Why
            pass

    parts = [cached[CACHE_COLUMNS]] if not cached.empty else []
    if live_name is not None:
        now = minute_series(Path(run_dir) / "ticks.csv")
        if now is not None and not now.empty:
            parts.append(now.assign(run=live_name))
    if not parts:
        return None

    out = pd.concat(parts, ignore_index=True)
    out = (out.drop_duplicates(subset="timestamp", keep="last")
              .sort_values("timestamp").reset_index(drop=True))
    return out[["timestamp", "price"]]


def series_for_run_dir(run_dir: Any) -> Optional[pd.DataFrame]:
    """The soak-wide price line for a run directory, derived from the directory.

    Works out the soak, the deployment and the fills to cover by reading what the
    run already wrote, so a caller with nothing but a path gets the same line the
    live writer produces.
    """
    # Tech: soak id from `session.jsonl`'s meta, sessions from `params.json`, and
    #       the fills to cover from `fills.csv`; anything missing simply narrows
    #       the result rather than failing it.
    # Why:  `python -m forecast_eval.reports <run_dir>` and the rebuild tool both
    #       need this and neither holds a `LiveConfig`. Deriving it here keeps the
    #       three callers on one answer — a re-render that disagreed with the
    #       run's own report would be worse than no re-render.
    from .sessions import SessionPolicy

    run_dir = Path(run_dir)
    soak_id = _soak_of(run_dir)

    policy = None
    params = run_dir / "params.json"
    if params.exists():
        try:
            p = json.loads(params.read_text(encoding="utf-8"))
            sessions = p.get("sessions")
            if sessions:
                # Why:  the slug picks `prices_<slug>.csv`; without it a hybrid
                #       run would read fullday's (both are DAY+NIGHT). Absent in
                #       older files, where None re-derives the same name.
                policy = SessionPolicy(sessions=tuple(sessions), slug=p.get("slug"))
        except Exception:                              # noqa: BLE001 — see Why
            policy = None
    if policy is None:
        # Why:  a directory from before `params.json` carried `sessions`, or a
        #       rebuilt historical one. NIGHT is the only deployment that has ever
        #       run live, so it is the honest default rather than a guess.
        policy = SessionPolicy.night()

    cover: List[pd.Timestamp] = []
    fills = run_dir / "fills.csv"
    if fills.exists():
        try:
            got = pd.read_csv(fills, usecols=["timestamp"])
            cover = list(pd.to_datetime(got["timestamp"], format="mixed"))
        except Exception:                              # noqa: BLE001 — see Why
            cover = []

    return soak_price_series(run_dir.parent, policy=policy, soak_id=soak_id,
                             cover=cover, run_dir=run_dir)
