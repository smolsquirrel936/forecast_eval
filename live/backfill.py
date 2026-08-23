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
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

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


# Tech: the cache's schema — the bar columns plus which month each came from.
# Why:  the collapsed output alone throws away the contract identity, and with it
#       any chance of re-deriving the series under a different splice rule. The
#       column costs nothing and is what makes the rule revisable after the fact
#       — including on live NIGHT data, the one place the real roll behaviour
#       will ever show up (the evaluated files have one contract per minute).
BAR_COLUMNS = ["timestamp", "price", "volume", "contract"]

# Tech: how many trailing bars the local cache keeps.
# Why:  the model's context is 3 008 bars; ten times that covers a roll plus any
#       restart around it while keeping the file small enough to rewrite on every
#       backfill. A cache that grew without bound would eventually cost more to
#       read than the venue call it exists to replace.
CACHE_KEEP_BARS = 30_000


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


def collapse_front_month(
    frames: Sequence[pd.DataFrame],
    *,
    front_month_at: Optional[Callable[[Any], str]] = None,
) -> pd.DataFrame:
    """Merge per-contract bar frames into one front-month-continuous series.

    With `front_month_at`, each minute is taken from the month that rule says was
    front — falling back to the most-traded row when that month did not print.
    Without it, the most-traded row wins outright.
    """
    # Tech: concatenate; rank each row by (is the calendar's front month, volume)
    #       and keep the best per timestamp.
    # Why:  MEASURED 2026-08-23. All three RPT files — the night file the pinned
    #       config was evaluated on included — carry **exactly one contract per
    #       minute**, so `load_minute_bars`'s highest-volume collapse never chose
    #       anything: the evaluated series is spliced by *calendar*, switching one
    #       day after each expiry. Volume was therefore never the convention, and
    #       a calendar rule is the one that matches.
    #
    #       It also has to be the same rule the live stream switches on. At the
    #       roll moment the expiring month usually still has the larger volume, so
    #       a volume rule would keep feeding the cache a contract we had already
    #       stopped trading — the warm-up context and the tape would disagree for
    #       a day and a half every month, and nothing would detect it.
    #
    #       Volume stays as the tie-break rather than being removed: a minute the
    #       front month did not print is better filled from the other month than
    #       dropped, and that is exactly what the fallback does.
    usable = [f for f in frames if f is not None and not f.empty]
    if not usable:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = pd.concat(usable, ignore_index=True)
    if front_month_at is not None and "contract" in df.columns:
        wanted = df["timestamp"].map(front_month_at).astype(str)
        df = df.assign(_is_front=(df["contract"].astype(str) == wanted))
        df = df.sort_values(["timestamp", "_is_front", "volume"])
        df = df.drop_duplicates(subset="timestamp", keep="last")
        df = df.drop(columns="_is_front")
    else:
        df = df.sort_values(["timestamp", "volume"])
        df = df.drop_duplicates(subset="timestamp", keep="last")
    return df.sort_values("timestamp").reset_index(drop=True)


def load_bar_cache(path: "str | Path") -> pd.DataFrame:
    """Read a bar cache written by `save_bar_cache`; empty frame when absent."""
    # Tech: read the CSV and parse `timestamp`; any read failure returns empty.
    # Why:  the cache is an optimisation, never a dependency — a corrupt or
    #       half-written file must degrade the warm-up to "what the venue can
    #       still serve", not stop a start-up. It is written by a process that can
    #       be killed at any moment, so a truncated tail is an expected state.
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=BAR_COLUMNS)
    try:
        # Tech: the contract column is read as text, never inferred.
        # Why:  a delivery month is an identifier that happens to look like a
        #       number. Letting pandas type it as int64 makes the cache fail to
        #       round-trip and, worse, makes `202608` and `"202608"` two different
        #       keys to the splice rule that compares them.
        df = pd.read_csv(p, parse_dates=["timestamp"], dtype={"contract": str})
    except Exception:                                  # noqa: BLE001 — see Why
        return pd.DataFrame(columns=BAR_COLUMNS)
    for col in ("timestamp", "price", "volume"):
        if col not in df.columns:
            return pd.DataFrame(columns=BAR_COLUMNS)
    # Tech: a cache written before the `contract` column existed still loads,
    #       with the column filled blank.
    # Why:  the cache is the only copy of a delisted month's bars, so a schema
    #       change must never be a reason to throw one away.
    if "contract" not in df.columns:
        df = df.assign(contract="")
    return df.assign(contract=df["contract"].fillna(""))[BAR_COLUMNS]


def save_bar_cache(path: "str | Path", bars: pd.DataFrame,
                   *, keep: int = CACHE_KEEP_BARS) -> None:
    """Persist the newest `keep` bars so a later run can splice across a roll."""
    # Tech: write the trailing `keep` rows to CSV, creating the parent directory.
    # Why:  **expired contracts are delisted** (§2.8 Finding D), so the month that
    #       just settled cannot be fetched from the venue at all — the splice is
    #       only possible from bars kept locally *before* it expired. A bounded
    #       tail is enough: the context is 3 008 bars and the cap is an order of
    #       magnitude above that, which covers a roll plus any restart around it
    #       without letting the file grow for the life of the deployment.
    if bars is None or bars.empty:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    bars.tail(keep).to_csv(p, index=False)


def fetch_bars_continuous(
    api: Any,
    contracts: Sequence[Any],
    start: str,
    end: str,
    *,
    policy: SessionPolicy,
    cache_path: Optional["str | Path"] = None,
    front_month_at: Optional[Callable[[Any], str]] = None,
    now: Optional[Any] = None,
) -> pd.DataFrame:
    """Fetch every contract's bars and splice them into one continuous series.

    `contracts` is the front month plus whatever earlier months are still
    listed. `cache_path`, when given, is merged in first and rewritten after —
    that is what carries an expired month's bars across its delisting.
    """
    # Tech: fetch each contract in turn, merge with the cache, collapse to one
    #       row per minute by volume, rewrite the cache, and return the window.
    # Why:  the roll is the case this exists for. A restart the day after a
    #       settlement can reach only the *new* month, whose own history before
    #       the roll is thin back-month trading — 3 008 of those bars span far
    #       more calendar time than the ~2 days the model expects, and the loop
    #       would warm up on a series it was never evaluated against. Cache plus
    #       fetch, collapsed on volume, reproduces the offline loader's series
    #       across the boundary instead.
    #
    #       A failed fetch for one contract is recorded by the caller, not raised:
    #       the front month is the one that matters and an older month that has
    #       just been delisted is *expected* to fail. Losing it degrades the
    #       splice to what the cache holds, which is the whole reason it exists.
    frames = []
    if cache_path is not None:
        frames.append(load_bar_cache(cache_path))
    for contract in contracts:
        try:
            got = fetch_bars(api, contract, start, end, policy=policy, now=now)
        except Exception:                              # noqa: BLE001 — see Why
            continue
        # Tech: stamp every fetched bar with the month it came from.
        # Why:  `kbars_to_bars` never sees the contract — it converts a payload.
        #       Stamping here is what lets the splice rule and the cache know
        #       whose minute is whose, which is the whole basis of both.
        frames.append(got.assign(
            contract=str(getattr(contract, "delivery_month", ""))))
    merged = collapse_front_month(frames, front_month_at=front_month_at)
    if cache_path is not None:
        save_bar_cache(cache_path, merged)
    # Tech: the contract column is cached but not returned.
    # Why:  the warm-up path downstream expects the three-column bar frame that
    #       `load_minute_bars` produces, and widening it here would silently
    #       change what `LiveRuntime.warm_up` and the loop's history buffers see.
    return merged.drop(columns=["contract"], errors="ignore")
