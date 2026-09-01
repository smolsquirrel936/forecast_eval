"""TickCapture — the raw feed, written down before anything touches it.

This is the input half of V1's live validation. The offline half already proved
`TickAggregator` reproduces the RPT convention; the remaining question is whether
the *live tick stream* aggregates to the same bars `kbars` reports for the same
session. Answering it needs the ticks exactly as they arrived.

**Captured before filtering, deliberately.** simtrade prints and out-of-session
ticks are written too, even though the aggregator drops both. Without them the
file could not be used to verify that the dropping happened correctly — and a
capture that only records what survived the filter cannot audit the filter.

**Two clocks per row.** `timestamp` is the exchange's; `received` is ours, taken
when the loop could first act on the tick. Bars are built on the first and closed
on the second, and the gap between them is what the §2.16 flush grace has to
cover — so the file records both rather than leaving that gap unobservable.

Separate from `Recorder` because the volume profile is different: the recorder
flushes every line so a kill loses at most one event, which is right for a few
hundred decisions per session but wrong for a tick stream. This buffers and
flushes periodically; losing the tail of a diagnostic file is acceptable in a way
that losing a fill record is not.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

FIELDS = ["timestamp", "received", "code", "price", "volume", "simtrade"]


class TickCapture:
    def __init__(self, path: "str | Path", *, flush_every: int = 500):
        # Tech: open a CSV, write the header, and buffer rows between flushes.
        # Why:  CSV rather than JSONL because the consumer is a bar-by-bar diff in
        #       pandas, and because a tick stream is uniform — the per-event
        #       flexibility JSONL buys is worth nothing here and costs size.
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(FIELDS)
        self.flush_every = flush_every
        self.n = 0
        self.n_simtrade = 0

    def write(self, timestamp: Any, price: float, volume: int = 1, *,
              simtrade: bool = False, code: str = "",
              received: Any = None) -> None:
        # Tech: append one row; flush on a fixed cadence. `received` defaults to
        #       the local clock at the moment of writing.
        # Why:  `code` is recorded per tick because the contract behind an alias
        #       can change at rollover (V1b) — a session that spans one would
        #       otherwise be indistinguishable from one that did not.
        #
        #       `received` exists to make the §2.16 flush grace measurable rather
        #       than estimated. The grace has to cover the gap between a tick's
        #       *exchange* timestamp and the moment the loop can actually act on
        #       it; `timestamp` alone cannot show that gap, so the 2 s default was
        #       sized against a proxy. Stamping at *write* time is the right
        #       measure because `write` is called from `LiveRuntime.on_tick`, on
        #       the same single consumer thread as `on_clock` — so this instant is
        #       exactly what races the flush, and it includes queue backlog as
        #       well as network lag. An enqueue-time stamp would miss the backlog
        #       half. The parameter exists so tests can pin it.
        self._writer.writerow([
            _iso(timestamp),
            _iso(datetime.now() if received is None else received),
            code, float(price), int(volume), int(bool(simtrade)),
        ])
        self.n += 1
        if simtrade:
            self.n_simtrade += 1
        if self.n % self.flush_every == 0:
            self._fh.flush()

    def close(self) -> None:
        # Tech: flush and close; safe to call twice.
        # Why:  the final partial buffer is the part most likely to matter — it
        #       holds the session's last minutes, which is where end-of-session
        #       behaviour gets audited.
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "TickCapture":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _iso(ts: Any) -> str:
    # Tech: ISO-8601 with microseconds where available.
    # Why:  round-trips losslessly through `pd.to_datetime`, and sorts as text —
    #       which makes the file greppable without parsing it.
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


def read_ticks(path: "str | Path") -> pd.DataFrame:
    """Read a capture back, typed, for the V1 aggregation diff."""
    # Tech: parse timestamps as ISO-8601 with a *variable* fractional part, then
    #       coerce the simtrade flag back to bool.
    # Why:  the consumer replays these through `TickAggregator`, which expects the
    #       same types the live callback delivered; leaving `simtrade` as 0/1 ints
    #       would make the filter silently treat every tick as genuine.
    #
    #       `format="ISO8601"` is required, not cosmetic (§2.17). `_iso` writes
    #       `ts.isoformat()`, which **omits the fraction entirely when microseconds
    #       are zero** — so any real session mixes `...T15:00:00` with
    #       `...T15:00:00.024000`. Bare `pd.to_datetime` infers the format from the
    #       first row and then raises on the first row that differs, which made
    #       every real capture unreadable while uniform test fixtures passed.
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601")

    # Tech: parse `received` when the file carries it, and leave it out when not.
    # Why:  the column was added 2026-08-11, after two live sessions had already
    #       been captured — including the one holding the only observed simtrade
    #       prints. Those files are evidence the V1 diff still reads, so the
    #       reader must not reject a capture for predating the column.
    if "received" in df.columns:
        df["received"] = pd.to_datetime(df["received"], format="ISO8601")

    df["simtrade"] = df["simtrade"].astype(bool)
    df["code"] = df["code"].fillna("").astype(str)
    return df


def price_series(path: "str | Path") -> Optional[pd.DataFrame]:
    """A capture as the `(timestamp, price)` frame the report charts against.

    Returns None when there is no capture to read.
    """
    # Tech: read the capture through `read_ticks`, drop simtrade prints, and keep
    #       the two columns `reports.generate_report` wants.
    # Why:  the price line and the buy-and-hold curve are drawn from this, and
    #       without it the report's "Price with fills" panel has fills plotted
    #       against nothing — which is what a live run produced until 2026-09-02,
    #       because `write_all` was called with no `report_ticks` at all.
    #
    #       Simtrade prints are excluded for §2.5's reason: they are not evidence
    #       the market traded, so a chart drawn from them would not be the series
    #       the run actually saw. Going through `read_ticks` rather than a second
    #       `read_csv` is what keeps the ISO-8601 handling above in one place —
    #       it was rediscovered once already, in `tools/session_artifacts.py`.
    p = Path(path)
    if not p.exists():
        return None
    try:
        df = read_ticks(p)
    except Exception:                                  # noqa: BLE001 — see Why
        # Why:  a capture is written by a process that can be killed mid-row, and
        #       this is only ever the *background* of a chart. A malformed tail
        #       must cost the price line, never the report.
        return None
    df = df[~df["simtrade"]]
    if df.empty:
        return None
    return pd.DataFrame({"timestamp": df["timestamp"].to_numpy(),
                         "price": df["price"].astype(float).to_numpy()})
