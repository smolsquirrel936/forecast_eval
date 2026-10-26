"""A live run's directory, in the backtest's own artifact schema.

Until now a live run left `session.jsonl` and `ticks.csv`, and a backtest left
`fills`, `orders`, `forecasts`, `signals`, `trades` and `params.json` plus a
rendered report. The two were not comparable without writing a projection, and
`reports.generate_report` — which only ever wanted three CSVs in a directory —
could not be pointed at a soak at all.

**Nothing here re-derives those files.** `LiveLoop.result()` already returns a
`BacktestResult`, built for exactly this reason ("every metric, report and log
writer already consumes `BacktestResult`; reusing it means a live session is
analysable with the exact same tooling"), so this module hands that object to
`logging_io.write_all` — the *same* function the backtest calls. The schemas
cannot drift, because there is only one writer.

What this module adds is *when*: a soak runs for weeks, so the artifacts have to
be current while it runs rather than only at the end, and they must not be
rewritten once a second for the ten hours a day the market is shut.
"""
from __future__ import annotations

import time as _time
import hashlib
import subprocess

from pathlib import Path
from typing import Any, Mapping, Optional

from ..logging_io import write_all, write_params
from .capture import price_series
from .prices import soak_price_series

# Tech: the shortest gap between two artifact writes.
# Why:  the stride is 30 bars — one forecast and one signal every 30 minutes —
#       so a minute's resolution is already far finer than the data changes.
#       This exists to bound I/O, not to bound staleness; the change gate below
#       is what actually decides whether a write happens.
MIN_INTERVAL_SECONDS = 60.0

# Tech: the artifact file extension.
# Why:  `write_all` defaults to `.parquet` with a CSV fallback, so the format a
#       run lands in would depend on whether a parquet engine happened to be
#       installed. `real_data.py` — the CLI every recorded backtest in
#       `results.tsv` came from — pins `.csv` explicitly, and a soak's directory
#       is meant to be diffable against those without a format conversion first.
ARTIFACT_EXT = ".csv"


def strategy_sha(package: str) -> str:
    # Tech: sha256 over every `.py` in `strategy/<package>/`, in sorted path
    #       order, with newlines normalised to LF; first 12 hex.
    # Why:  LIVE_STRATEGY_ROUTING_PLAN Phase 5. The package IS the strategy now,
    #       so "which strategy traded" is answered by the package's content, and
    #       the git commit alone does not answer it -- an uncommitted edit is
    #       exactly the state a research loop spends most of its time in.
    #
    #       Newlines are normalised deliberately. `tests/test_strategy_isolation.py`
    #       pins raw bytes and fails on this Windows checkout purely because
    #       `autocrlf=true` rewrites line endings; a provenance hash that changes
    #       when a file is checked out on a different platform records the
    #       checkout, not the strategy.
    root = Path(__file__).resolve().parent.parent / "strategy" / package
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        h.update(path.relative_to(root).as_posix().encode())
        # `read_text` opens in universal-newline mode, so CRLF arrives as LF and
        # the hash is the same on every checkout. Reading bytes would not be.
        h.update(path.read_text(encoding="utf-8").encode("utf-8"))
    return h.hexdigest()[:12]


def git_commit() -> Optional[str]:
    # Tech: `git rev-parse --short=12 HEAD`, or None when it cannot be read.
    # Why:  the other half of "what ran". None rather than a raise because a live
    #       run must not die for want of a provenance string -- a deployment from
    #       an exported tree with no `.git` is unusual but not an error, and the
    #       `strategy_sha` above still identifies the strategy in that case.
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=5, check=True)
        return out.stdout.strip() or None
    except Exception:                                  # noqa: BLE001 -- see Why
        return None


def git_dirty() -> Optional[bool]:
    # Tech: True when the working tree has uncommitted changes, None when git
    #       cannot be read.
    # Why:  `git_commit` alone MISDESCRIBES a run made from a dirty tree, which
    #       is most of them during development -- the first live run to carry
    #       these fields reported commit `af4be0b` while every live module it
    #       executed was modified and unstaged. `strategy_sha` covers
    #       `strategy/`; this covers everything else. A boolean rather than a
    #       diff: the question a reader asks later is "can I trust the commit?",
    #       and one bit answers it.
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=10, check=True)
        return bool(out.stdout.strip())
    except Exception:                                  # noqa: BLE001 -- see git_commit
        return None


def live_params(config: Any, *, strategy_params: Mapping[str, Any],
                out_dir: Any, broker: str,
                strategy_origins: Optional[Mapping[str, str]] = None,
                warmup_days: Optional[int] = None,
                soak_id: Optional[str] = None,
                extra: Optional[Mapping[str, Any]] = None) -> dict:
    """The `params.json` payload for a live run, in the backtest's key order."""
    # Tech: read the strategy fields from the run's RESOLVED strategy params and
    #       add the four that only a live run has (broker, warm-up window,
    #       deployment name, soak).
    # Why:  LIVE_STRATEGY_ROUTING_PLAN D2 -- these used to come off `LiveConfig`,
    #       which held a copy of the package's values. `strategy_params` is the
    #       resolution the run's components were actually built from, `--param`
    #       overrides included, so `params.json` records what ran rather than what
    #       a second declaration said would run.
    # Why:  a backtest's `params.json` answers "what produced these numbers", and
    #       for a live run that question has two halves — the pinned strategy,
    #       which must be written in the *same keys* so the two files can be
    #       diffed, and the operational context, which the backtest has no
    #       concept of. Keeping the shared keys identical is the point: a report
    #       reading `params.json` for its Settings block gets the same fields
    #       from either kind of run.
    params = {
        "data": "live",
        "source_commit": config.source_commit,
        "source_calmar": config.source_calmar,
        "source_dataset": config.source_dataset,
        "checkpoint": strategy_params["checkpoint"],
        "context_length": strategy_params["context_length"],
        "horizon_bars": strategy_params["horizon_bars"],
        "stride_bars": strategy_params["stride_bars"],
        "buy_threshold": strategy_params["buy_threshold"],
        "sell_threshold": strategy_params["sell_threshold"],
        "aggression_ticks": strategy_params["aggression_ticks"],
        "min_prob": strategy_params["min_prob"],
        "max_aggression_ticks": strategy_params["max_aggression_ticks"],
        "max_position": strategy_params["max_position"],
        "forced_close": strategy_params["forced_close"],
        "sessions": sorted(config.policy.sessions),
        # Tech: the deployment's file slug (`fills_<slug>.csv`, `prices_<slug>.csv`).
        # Why:  `sessions` no longer names the files -- hybrid and fullday share
        #       DAY+NIGHT -- so a re-render reading this file needs the slug.
        "slug": config.policy.cache_slug(),
        "roll_at": str(config.policy.roll_at_time()),
        "broker": broker,
        # Tech: what produced this strategy -- the package, its content hash, the
        #       repo commit, where each knob came from, and one boolean.
        # Why:  LIVE_STRATEGY_ROUTING_PLAN Phase 5. Removing the provenance GATE
        #       (D1) is only defensible if the provenance RECORD is complete, and
        #       "complete" now means the package, because that is where the
        #       strategy lives. `strategy_overridden` is a boolean rather than
        #       something a reader must derive from `strategy_origins`, because
        #       the one question asked of this file later is "is this row
        #       comparable to a `results.tsv` row?" -- and if anything was typed,
        #       it is not.
        "strategy_package": config.strategy_package,
        "strategy_sha": strategy_sha(config.strategy_package),
        "git_commit": git_commit(),
        "git_dirty": git_dirty(),
        "strategy_origins": dict(strategy_origins or {}),
        "strategy_overridden": any(v == "param"
                                   for v in (strategy_origins or {}).values()),
        "warmup_days": warmup_days,
        "soak_id": soak_id,
        "output_dir": str(out_dir),
    }
    if extra:
        params.update(extra)
    return params


class ArtifactWriter:
    """Keeps a live run's directory current, in the backtest's schema."""

    def __init__(self, out_dir: Any, *,
                 min_interval_seconds: float = MIN_INTERVAL_SECONDS,
                 ext: str = ARTIFACT_EXT,
                 policy: Any = None, soak_id: Optional[str] = None):
        self.out_dir = Path(out_dir)
        # Tech: the deployment and soak this run belongs to, for the price line.
        # Why:  both are optional so the writer still works standalone (the tests
        #       drive it from a bare `LiveLoop`); without a policy there is no
        #       `prices_<slug>.csv` to key, so it falls back to this run's own
        #       capture — which is what it did before the soak-wide line existed.
        self.policy = policy
        self.soak_id = soak_id
        self.min_interval = float(min_interval_seconds)
        self.ext = ext
        # Tech: None, not 0.0, as the "never written" sentinel.
        # Why:  `time.monotonic()` counts from BOOT and starts near zero on a
        #       freshly booted machine, so a 0.0 sentinel makes the first write
        #       look throttled (`now - 0.0 < min_interval`) and it never lands.
        #       Caught 2026-10-14 by the gate tests on a 6-minute-old boot; the
        #       same rule `runtime.py`'s `_ladder_last` already follows.
        self._last_write: Optional[float] = None
        self._last_counts: Optional[tuple] = None
        self.writes = 0
        self.failures = 0

    def write_params(self, params: Mapping[str, Any]) -> Path:
        # Tech: write `params.json` on its own, at start-up.
        # Why:  it is the one artifact that is known before any bar arrives, and
        #       a run that dies in its first minute should still say what it was
        #       trying to be.
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return write_params(dict(params), self.out_dir / "params.json")

    def _counts(self, runtime: Any) -> tuple:
        # Tech: the four list lengths that decide whether anything is new.
        # Why:  a soak spends ten hours a day outside its session, where no bar
        #       arrives and every artifact would be rewritten byte-for-byte. The
        #       gate is on *content*, not on the clock, so the dead zone costs
        #       nothing and a busy session is never skipped.
        loop = runtime.loop
        return (len(loop.fills), len(loop.orders),
                len(loop.forecasts), len(loop.signals))

    def write(self, runtime: Any, *, report: bool = False,
              force: bool = False) -> Optional[dict]:
        """Write every artifact if something changed; return the paths, or None.

        `force` writes regardless (shutdown); `report` also renders the charts.
        """
        # Tech: skip when throttled or unchanged, unless forced; otherwise hand
        #       the loop's own `BacktestResult` to `write_all`.
        # Why:  `write_all` is the backtest's writer, called with the backtest's
        #       arguments — including `roll_fills`, without which `build_trades`
        #       books a rolled position as two round trips instead of one and the
        #       live `trades.csv` would disagree with the backtest's for a reason
        #       that has nothing to do with trading.
        now = _time.monotonic()
        counts = self._counts(runtime)
        if not force:
            if (self._last_write is not None
                    and now - self._last_write < self.min_interval):
                return None
            if counts == self._last_counts:
                self._last_write = now
                return None

        # Tech: any failure is swallowed, counted, and reported by the caller.
        # Why:  these files are a *record* of a live run, never an input to it. A
        #       full disk or a locked file must not be able to stop a process
        #       that is holding a position — the same reasoning `write_all`
        #       already applies to a failing chart backend, one level up.
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            # Tech: hand the report this run's own capture as the price series,
            #       and only when actually rendering.
            # Why:  MEASURED 2026-09-02 on a real run. `write_all` was called
            #       with no `report_ticks`, so "Price with fills" drew the fills
            #       against an empty axis — the panel rendered, which is why it
            #       took a reader to notice rather than a failure. The capture is
            #       sitting in this same directory. Loading it only under
            #       `report` keeps a 20 MB read off the once-a-minute path.
            result = runtime.loop.result()
            written = write_all(
                result, self.out_dir, ext=self.ext,
                auto_report=report,
                report_ticks=(self._price_line(result) if report else None),
                roll_fills=(getattr(runtime, "roll_fills", None) or None),
            )
        except Exception:                                  # noqa: BLE001 — see Why
            self.failures += 1
            return None

        self._last_write = now
        self._last_counts = counts
        self.writes += 1
        return written

    def _price_line(self, result: Any):
        # Tech: the soak's price series when a soak is known, else this run's own
        #       capture; the fills' timestamps are handed over as `cover`.
        # Why:  a restart opens a new directory, so a per-run chart shows minutes
        #       of prices under fills that span weeks — and a `--resume` replays
        #       fills executed in an *earlier* directory, which the current
        #       capture cannot reach at all. `cover` is what pulls those runs in:
        #       a capture spanning a fill we are charting is, by definition, the
        #       one holding that fill's prices.
        try:
            return soak_price_series(
                self.out_dir.parent, policy=self.policy, soak_id=self.soak_id,
                cover=[f.timestamp for f in result.fills],
                run_dir=self.out_dir)
        except Exception:                              # noqa: BLE001 — see Why
            # Why:  the chart background is the least important thing here. Any
            #       failure degrades to this run's own capture, and then to no
            #       line at all, rather than costing the artifacts.
            return price_series(self.out_dir / "ticks.csv")

    def summary(self) -> dict:
        return {"artifact_writes": self.writes,
                "artifact_failures": self.failures}
