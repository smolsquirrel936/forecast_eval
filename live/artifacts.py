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
from pathlib import Path
from typing import Any, Mapping, Optional

from ..logging_io import write_all, write_params
from .capture import price_series

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


def live_params(config: Any, *, out_dir: Any, broker: str,
                warmup_days: Optional[int] = None,
                soak_id: Optional[str] = None,
                extra: Optional[Mapping[str, Any]] = None) -> dict:
    """The `params.json` payload for a live run, in the backtest's key order."""
    # Tech: read the strategy fields off the `LiveConfig` and add the four that
    #       only a live run has (broker, warm-up window, deployment name, soak).
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
        "checkpoint": config.checkpoint,
        "context_length": config.context_length,
        "horizon_bars": config.forecast_horizon_bars,
        "stride_bars": config.forecast_stride_bars,
        "buy_threshold": config.buy_threshold,
        "sell_threshold": config.sell_threshold,
        "aggression_ticks": config.aggression_ticks,
        "min_prob": config.min_prob,
        "max_aggression_ticks": config.max_aggression_ticks,
        "max_position": config.max_position,
        "forced_close": config.forced_close_on_session_end,
        "sessions": sorted(config.policy.sessions),
        "roll_at": str(config.policy.roll_at_time()),
        "broker": broker,
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
                 ext: str = ARTIFACT_EXT):
        self.out_dir = Path(out_dir)
        self.min_interval = float(min_interval_seconds)
        self.ext = ext
        self._last_write = 0.0
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
            if now - self._last_write < self.min_interval:
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
            written = write_all(
                runtime.loop.result(), self.out_dir, ext=self.ext,
                auto_report=report,
                report_ticks=(price_series(self.out_dir / "ticks.csv")
                              if report else None),
                roll_fills=(getattr(runtime, "roll_fills", None) or None),
            )
        except Exception:                                  # noqa: BLE001 — see Why
            self.failures += 1
            return None

        self._last_write = now
        self._last_counts = counts
        self.writes += 1
        return written

    def summary(self) -> dict:
        return {"artifact_writes": self.writes,
                "artifact_failures": self.failures}
