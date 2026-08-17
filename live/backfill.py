"""Backfill — turn Shioaji `kbars` into the bar frame the model was evaluated on.

Warm-up and live trading draw bars from *different* sources: live bars come from
the tick stream via `TickAggregator`, warm-up bars come from `api.kbars`. Both
must land on the identical convention or the model's context at every process
start is subtly different from everything it was evaluated on — and 3 008 of the
first bars after a restart come from *this* path (SHIOAJI_PLAN.md §7.5).

The target convention, measured from `TXF_RPT_minute_day.csv` (§2.4):

  * 1 minute, **left-labelled** — bar `T` covers `[T, T+1min)`;
  * `price` is the bar's **close**;
  * **minutes with no trading are absent entirely** — the file contains no
    zero-volume bar, so any zero-volume row `kbars` returns is filler that must
    be dropped rather than fed to the model as a real observation.

**`kbars` does not match it, and the difference was invisible until measured.**
V1c against the live API (2026-08-10) established:

  * **`kbars` is RIGHT-labelled** — a day session returns `08:46 … 13:45` where
    RPT returns `08:45 … 13:44`. Same 300 intervals, labels shifted one minute.
    Corrected here by `source_label="right"` (the default).
  * **`kbars` carries the night session too** (00:00–23:59); RPT is day-only.
    Corrected by `sessions=("DAY",)` on `fetch_bars`.
  * Empty minutes *are* omitted, matching RPT — no correction needed.

A third mismatch was measured later, during the V3 60-minute run (2026-08-11):

  * **`kbars` returns the in-progress minute as a finished bar.** A fetch at
    04:11:29 came back with a final bar covering only 04:11:00–04:11:29, which
    the live aggregator then re-emitted in full — putting one timestamp into the
    model context twice. Corrected by `fetch_bars`, which keeps only bars whose
    minute has fully elapsed (§2.14).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Sequence

import pandas as pd

from .bars import BAR_LENGTH
from .sessions import SessionPolicy

# Tech: the column names Shioaji's KBars exposes.
# Why:  named once so the duck-typed extraction below reads as a mapping rather
#       than scattered string literals; only ts/Close/Volume are actually needed,
#       but the rest are pulled when present for the V1c diff report.
_TS = "ts"
_CLOSE = "Close"
_VOLUME = "Volume"


def _columns(kbars: Any) -> Dict[str, Any]:
    # Tech: extract the KBars payload as a plain dict of columns, accepting a
    #       mapping, an object with attributes, or anything dict-unpackable.
    # Why:  Shioaji's KBars is documented as usable via `pd.DataFrame({**kbars})`,
    #       but the concrete type is unverified here and tests supply a plain
    #       dict. Duck-typing all three shapes means this module needs no
    #       `shioaji` import and no guess about the class.
    if isinstance(kbars, dict):
        return dict(kbars)
    try:
        return {**kbars}
    except TypeError:
        pass
    out = {}
    for name in (_TS, "Open", "High", "Low", _CLOSE, _VOLUME, "Amount"):
        value = getattr(kbars, name, None)
        if value is not None:
            out[name] = value
    return out


def kbars_to_bars(
    kbars: Any,
    *,
    drop_zero_volume: bool = True,
    policy: Optional[SessionPolicy] = None,
    source_label: str = "right",
) -> pd.DataFrame:
    """Convert a Shioaji KBars payload to the `timestamp, price, volume` frame.

    Output is schema-identical to `real_data.load_minute_bars`, so the result can
    be pushed straight into `LiveLoop`'s history buffers or handed to
    `Environment` unchanged.

    `policy` filters to the sessions that policy trades; `None` keeps every bar.
    The converter stays session-agnostic — `fetch_bars` *requires* a policy, so
    the session is always an explicit decision by the caller.

    `source_label` says how the *input* is labelled. `"right"` is what Shioaji
    actually sends (measured, see below) and triggers the corrective shift;
    `"left"` means the payload is already on the RPT convention and is left
    alone — used by `v1c_check` to read raw labels, and by tests that are
    exercising something other than labelling.
    """
    cols = _columns(kbars)
    if _TS not in cols or _CLOSE not in cols:
        raise ValueError(
            f"kbars payload missing required columns; got {sorted(cols)}"
        )

    # Tech: build the frame and coerce `ts` to datetimes, treating integers as
    #       nanosecond epochs.
    # Why:  Shioaji returns `ts` as epoch nanoseconds; passing those through
    #       `pd.to_datetime` without a unit would be interpreted inconsistently
    #       across pandas versions. Values that are already datetimes are left
    #       alone so a test (or a future API change) can supply either.
    df = pd.DataFrame(cols)
    ts = df[_TS]
    if pd.api.types.is_numeric_dtype(ts):
        df[_TS] = pd.to_datetime(ts, unit="ns")
    else:
        df[_TS] = pd.to_datetime(ts)

    # Tech: when the source is right-labelled, move every timestamp back one
    #       minute. Done here — before session filtering and before any drop — so
    #       every later step sees RPT-convention labels.
    # Why:  MEASURED 2026-08-10 against the live API. A TXF day session runs
    #       08:45–13:45; `kbars` returned `first=08:46 last=13:45` (300 bars),
    #       i.e. bar `T` covers `(T-1min, T]`. `TXF_RPT_minute_day.csv` returns
    #       08:45…13:44 for the same session, i.e. `[T, T+1min)`. The two describe
    #       the *same* intervals under different labels, so subtracting a minute
    #       converts one to the other exactly — no data moves, only its name.
    #
    #       Ordering matters and is not cosmetic. Filtering first would test
    #       `classify_session` against the wrong labels: DAY is `08:45 <= t <
    #       13:45`, so the unshifted 13:45 bar — the session's genuine last
    #       minute — classifies as None and would be silently dropped, costing one
    #       bar every single day, while the 299 survivors would each be
    #       misattributed by a minute.
    if source_label == "right":
        df[_TS] = df[_TS] - pd.Timedelta(minutes=1)
    elif source_label != "left":
        raise ValueError(f"source_label must be 'right' or 'left', "
                         f"got {source_label!r}")

    # Tech: drop NaN closes, then drop zero-volume rows unless told otherwise.
    # Why:  NaN handling mirrors `load_minute_bars` (Toto2 propagates NaN). The
    #       zero-volume drop is what aligns `kbars` to the RPT file's "empty
    #       minutes are absent" rule — the file provably contains no zero-volume
    #       bar, so a filler row here would be an observation the model was never
    #       trained or evaluated against.
    df = df.dropna(subset=[_CLOSE])
    volume = df[_VOLUME] if _VOLUME in df.columns else 1
    if drop_zero_volume and _VOLUME in df.columns:
        df = df[df[_VOLUME] > 0]
        volume = df[_VOLUME]

    # Tech: when `sessions` is given, keep only bars whose label classifies into
    #       one of them.
    # Why:  MEASURED 2026-08-10 — `kbars` for TXFR1 returns 00:00–23:59, i.e. the
    #       day session *and* the night session, whereas
    #       `TXF_RPT_minute_day.csv` holds 08:45–13:45 only (verified across all
    #       1 124 790 rows). Feeding unfiltered kbars into the context would make
    #       3 008 bars mean ~2.6 mixed-session days instead of the ~10 day-session
    #       days the model was evaluated on — a structurally different input.
    if policy is not None:
        df = df[df[_TS].map(policy.includes)]

    # Tech: sort chronologically, reset the index, project to the three-column
    #       schema.
    # Why:  chronological order is the core invariant of the replay; projecting
    #       here means callers never have to know `kbars` carried OHLC at all.
    df = df.sort_values(_TS).reset_index(drop=True)
    return pd.DataFrame({
        "timestamp": df[_TS],
        "price": df[_CLOSE].astype(float),
        "volume": (df[_VOLUME].astype(float) if _VOLUME in df.columns
                   else pd.Series(1.0, index=df.index)),
    })


def fetch_bars(
    api: Any,
    contract: Any,
    start: str,
    end: str,
    *,
    policy: SessionPolicy,
    now: Optional[Any] = None,
) -> pd.DataFrame:
    """Fetch `[start, end]` 1-min bars and convert them. Dates are `YYYY-MM-DD`.

    Callers must have already charged the request to `RestartGuard`
    (`record_backfill`) and checked traffic headroom (`check_usage`) — over quota
    Shioaji returns *empty* history rather than erroring, so an unchecked call
    can silently yield an empty context.

    `now` is the moment the fetch is considered to happen; it decides which
    trailing bars are still in progress (see below). Defaults to `datetime.now()`
    and exists so tests can pin it.
    """
    # Tech: single kbars request over the whole span, converted in one pass and
    #       filtered to `policy`'s sessions. `policy` is REQUIRED — there is no
    #       default.
    # Why:  one call rather than day-by-day paging keeps the request count far
    #       under the 50-per-10s market-data limit; V1c measured 9 902 bars for a
    #       10-day span, so paging is unnecessary.
    #
    #       The missing default is deliberate and was paid for: this function
    #       previously defaulted to `("DAY",)` while the strategy had been tuned
    #       on NIGHT, and nothing could detect it — the backfill would simply have
    #       loaded the wrong session's history into the model's context forever.
    #       Requiring the caller to say which session it trades makes that class
    #       of mistake unrepresentable.
    kbars = api.kbars(contract, start=start, end=end)
    df = kbars_to_bars(kbars, policy=policy)

    # Tech: keep only bars whose whole minute has elapsed — bar `T` covers
    #       `[T, T+1min)`, so it is finished exactly when `T + 1min <= now`.
    # Why:  MEASURED 2026-08-11 (§2.14). `kbars` returns the **in-progress**
    #       minute as if it were a bar: a backfill at 04:11:29 came back with a
    #       final bar covering only 04:11:00–04:11:29. That bar is not an
    #       observation the model was ever evaluated against, and worse, the live
    #       aggregator then emits its *own* bar for the same minute — so the same
    #       timestamp entered the context twice, each copy holding a different
    #       fragment of the minute. Dropping it here removes the cause: the
    #       backfill ends at the last complete minute and the live stream owns
    #       everything after it, with no overlap. Comparing against the right edge
    #       rather than flooring `now` keeps the boundary exact — at 04:12:00 the
    #       04:11 bar *is* complete and is kept.
    if df.empty:
        return df
    cutoff = pd.Timestamp(datetime.now() if now is None else now)
    return df[df["timestamp"] + BAR_LENGTH <= cutoff].reset_index(drop=True)
